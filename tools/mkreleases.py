#!/usr/bin/env python3
"""Build this product's release artifacts into the repo's layout.

    tools/mkreleases.py [c] [rs] [--gnu] [--src] [--min-from VER]
                        [--allow-dirty] [--force] [--no-smoke]
                        [--c-src DIR] [--rs-src DIR]

For each variant (default: both) it builds the COMMITTED tree (git HEAD of
the source repo), packs the binary as <product>/<product> inside
  <product>/<variant>/binaries/<product>-<variant>-v<ver>-<arch>-linux-static.tar.gz
(the layout every updater unpacks with --strip-components=1), and records the
version in <product>/<variant>/meta.tsv.  Then run tools/mkmanifest.py, then
tools/sign.py.

The default artifact is FULLY STATIC (musl): no shared libraries at all, so
one binary runs on any x86_64 Linux — Ubuntu 24.04/26.04 (the servers this is
for), older machines, Alpine.  Its manifest row says libc "any".  The C
daemons are built in an Alpine container (tools/musl.Dockerfile, built as
ircbuild-musl:static on first use) with static OpenSSL + libcurl; the Rust
daemons are built on this host for the <arch>-unknown-linux-musl target
(`rustup target add x86_64-unknown-linux-musl` once).  A static binary does
not pick up distro OpenSSL/libcurl security fixes: a fix there means a new
release.

--gnu also builds a dynamic glibc artifact on THIS host (…-linux-gnu.tar.gz).
It uses the distro's OpenSSL/libcurl but only runs on hosts at least as new as
this one; the manifest lists the static artifact first, so updaters pick the
static one unless it is absent.

Every binary is smoke-tested: started in an empty directory it must print its
"No config file found" first-run hint.  That runs here and, when the image is
present, in ircbuild-rocky8:gcc8 — the old-machine test environment (Rocky 8,
glibc 2.28), not the target platform.  --no-smoke skips both.

The version is read from where the daemon reads it (bot.h/hub.h, and the
BOT_VERSION/HUB_VERSION literal in the Rust consts.rs), and both variants must
agree: one upgrade run names one version for the C and the Rust nodes.

A released artifact is immutable: an existing file with a different hash is
refused unless --force (its hash is in a manifest that may already be signed).
"""
import argparse
import gzip
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relcommon as rc  # noqa: E402

MUSL_IMAGE = "ircbuild-musl:static"
OLD_IMAGE = "ircbuild-rocky8:gcc8"   # old-machine smoke test only
SMOKE_TEXT = b"No config file found"
CACHE = os.path.expanduser(os.environ.get("XDG_CACHE_HOME", "~/.cache"))


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def run_build(cmd, **kw):
    """Run a compiler step quietly; on failure show the end of its output."""
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    if r.returncode != 0:
        tail = r.stdout.decode(errors="replace").splitlines()[-40:]
        print("\n".join("    | " + l for l in tail), file=sys.stderr)
        raise subprocess.CalledProcessError(r.returncode, cmd)


def out(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


# ------------------------------------------------------------------ source
def read_version(src, cfg):
    path = os.path.join(src, cfg["version_file"])
    with open(path) as f:
        m = re.search(cfg["version_re"], f.read())
    if not m:
        rc.die(f"no version literal found in {path}")
    ver = m.group(1).lstrip("v")
    if not rc.VERSION_OK.match(ver):
        rc.die(f"{path}: version {ver!r} is not a plain dotted version")
    return ver


def git_state(src, allow_dirty):
    """(commit, commit_time, dirty).  Refuses a dirty tree unless allowed."""
    try:
        head = out(["git", "-C", src, "rev-parse", "HEAD"]).strip()
    except subprocess.CalledProcessError:
        rc.die(f"{src} is not a git repository with a commit")
    ctime = int(out(["git", "-C", src, "log", "-1", "--format=%ct", "HEAD"]).strip())
    dirty = bool(out(["git", "-C", src, "status", "--porcelain",
                      "--untracked-files=no"]).strip())
    untracked = out(["git", "-C", src, "ls-files", "--others", "--exclude-standard"]).split()
    if untracked:
        print(f"  warning: {src} has untracked files a committed build would not include: "
              f"{', '.join(untracked[:6])}{' …' if len(untracked) > 6 else ''}", file=sys.stderr)
    if dirty and not allow_dirty:
        rc.die(f"{src} has uncommitted changes — commit (and push) first, "
               f"or pass --allow-dirty for a throwaway build")
    pushed = subprocess.run(["git", "-C", src, "merge-base", "--is-ancestor",
                             "HEAD", "origin/main"], capture_output=True).returncode == 0
    if not pushed:
        print(f"  warning: {src} HEAD is not on origin/main (as last fetched)", file=sys.stderr)
    return head, ctime, dirty


def tree_files(src, dirty, untracked=True):
    """Files of the release: HEAD's, or with --allow-dirty the working tree's —
    tracked plus untracked-but-not-ignored, so a new file not yet `git add`ed
    is built too (a committed release would miss it: add it before committing)."""
    if dirty:
        names = out(["git", "-C", src, "ls-files", "-z", "--cached"]
                    + (["--others", "--exclude-standard"] if untracked else [])).split("\0")
        return [n for n in names if n and os.path.lexists(os.path.join(src, n))]
    return None   # use git archive


def export_tree(src, dirty, dest):
    """The exact tree the release is built from, into dest."""
    if not dirty:
        p1 = subprocess.Popen(["git", "-C", src, "archive", "--format=tar", "HEAD"],
                              stdout=subprocess.PIPE)
        run(["tar", "-x", "-C", dest], stdin=p1.stdout)
        if p1.wait() != 0:
            rc.die(f"git archive failed in {src}")
        return
    for n in tree_files(src, dirty):
        s, d = os.path.join(src, n), os.path.join(dest, n)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        if os.path.islink(s):
            os.symlink(os.readlink(s), d)
        else:
            shutil.copy2(s, d)


# ------------------------------------------------------------------ build
def docker_user_args():
    """Rootless docker maps container root to us; rootful docker needs -u."""
    try:
        sec = out(["docker", "info", "-f", "{{.SecurityOptions}}"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        rc.die("docker is not available — the static C build runs in a container")
    if "rootless" in sec:
        return []
    return ["-u", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp"]


def have_image(image):
    return subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True).returncode == 0


def ensure_musl_image():
    if have_image(MUSL_IMAGE):
        return
    here = os.path.dirname(os.path.abspath(__file__))
    print(f"  building   {MUSL_IMAGE} (first use)")
    run(["docker", "build", "-q", "-t", MUSL_IMAGE, "-f",
         os.path.join(here, "musl.Dockerfile"), here], stdout=subprocess.DEVNULL)


def musl_target(arch):
    t = f"{arch}-unknown-linux-musl"
    libdir = subprocess.run(["rustc", "--print", "target-libdir", "--target", t],
                            capture_output=True, text=True).stdout.strip()
    if not libdir or not os.path.isdir(libdir):
        rc.die(f"the Rust {t} target is not installed — run: rustup target add {t}")
    return t


def build(prod, variant, cfg, work, libc, arch):
    """Build in work/src; return the path of the finished, stripped binary."""
    srcdir = os.path.join(work, "src")
    if variant == "c":
        binary = os.path.join(srcdir, cfg["binary"])
        if libc == "static":
            ensure_musl_image()
            run_build(["docker", "run", "--rm", *docker_user_args(), "-v", f"{srcdir}:/src",
                 "-w", "/src", MUSL_IMAGE, "sh", "-c",
                 f"{cfg['build_static']} && strip {cfg['binary']}"])
        else:
            run_build(["bash", "-c", f"{cfg['build']} && strip {cfg['binary']}"], cwd=srcdir)
        return binary

    target_dir = os.path.join(CACHE, f"{prod}-releases", f"cargo-{variant}")
    cargo = ["cargo", "build", "--release", "--locked", "--bin", cfg["cargo_bin"]]
    if libc == "static":
        t = musl_target(arch)
        cargo += ["--target", t]
        binary = os.path.join(target_dir, t, "release", cfg["cargo_bin"])
    else:
        binary = os.path.join(target_dir, "release", cfg["cargo_bin"])
    run_build(cargo, cwd=srcdir, env={**os.environ, "CARGO_TARGET_DIR": target_dir,
                                "CARGO_TERM_COLOR": "never"})
    run(["strip", binary])
    return binary


def host_libc():
    try:
        r = subprocess.run(["ldd", "--version"], capture_output=True, text=True)
        first = (r.stdout or r.stderr).splitlines()[0]
    except (FileNotFoundError, IndexError):
        first = ""
    return "musl" if "musl" in first.lower() else "gnu"


def elf_interp(binary):
    """True if the ELF asks for a dynamic loader (PT_INTERP), i.e. is not static."""
    import struct
    with open(binary, "rb") as f:
        hdr = f.read(64)
        if hdr[:4] != b"\x7fELF":
            rc.die(f"{binary} is not an ELF executable")
        if hdr[4] != 2:
            rc.die(f"{binary} is not a 64-bit ELF")
        end = "<" if hdr[5] == 1 else ">"
        phoff, = struct.unpack_from(end + "Q", hdr, 32)
        phentsize, phnum = struct.unpack_from(end + "HH", hdr, 54)
        for i in range(phnum):
            f.seek(phoff + i * phentsize)
            if struct.unpack(end + "I", f.read(4))[0] == 3:   # PT_INTERP
                return True
    return False


def glibc_needed(binary):
    """Highest GLIBC_x.y symbol version the binary requires, or None."""
    try:
        syms = out(["objdump", "-T", binary])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    vers = {tuple(int(x) for x in m.split(".")) for m in re.findall(r"GLIBC_([0-9]+(?:\.[0-9]+)+)", syms)}
    return max(vers) if vers else None


def smoke(binary, prod, where):
    """Start the binary in an empty directory; it must print its first-run hint."""
    with tempfile.TemporaryDirectory(prefix="mkrel-smoke-") as d:
        shutil.copy2(binary, os.path.join(d, prod))
        if where == "host":
            cmd = [os.path.join(d, prod)]
        else:
            cmd = ["docker", "run", "--rm", *docker_user_args(), "-v", f"{d}:/s",
                   "-w", "/s", where, f"/s/{prod}"]
        try:
            r = subprocess.run(cmd, cwd=d, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=60)
            got = r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            got = b"(timed out)"
    ok = SMOKE_TEXT in got
    label = "this host" if where == "host" else where
    print(f"  smoke      {label}: {'ok' if ok else 'FAILED'}")
    if not ok:
        rc.die(f"{prod} did not start on {label}: {got.decode(errors='replace').strip()[:300]}")


# ------------------------------------------------------------------ pack
def _tar_gz(members, mtime):
    """Deterministic tar.gz: fixed owner, mtime and member order, no gzip name/time."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as t:
        for name, data, mode in members:
            ti = tarfile.TarInfo(name)
            ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = mtime, 0, 0, "", ""
            if data is None:
                ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                t.addfile(ti)
            else:
                ti.size, ti.mode = len(data), mode
                t.addfile(ti, io.BytesIO(data))
    gz = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=gz, mtime=0) as g:
        g.write(raw.getvalue())
    return gz.getvalue()


def pack_binary(prod, binary, mtime):
    with open(binary, "rb") as f:
        data = f.read()
    return _tar_gz([(f"{prod}/", None, 0), (f"{prod}/{prod}", data, 0o700)], mtime)


def pack_source(src, dirty, prefix, mtime):
    if not dirty:
        return out_bytes(["git", "-C", src, "archive", "--format=tar.gz",
                          f"--prefix={prefix}/", "HEAD"])
    members = [(f"{prefix}/", None, 0)]
    # Tracked files only: an untracked file may be anything (editor or chat
    # history, local notes) and a source tarball is meant to be shared.
    for n in sorted(tree_files(src, dirty, untracked=False)):
        p = os.path.join(src, n)
        if os.path.isfile(p) and not os.path.islink(p):
            with open(p, "rb") as f:
                members.append((f"{prefix}/{n}", f.read(),
                                0o755 if os.access(p, os.X_OK) else 0o644))
    return _tar_gz(members, mtime)


def out_bytes(cmd):
    return subprocess.run(cmd, check=True, capture_output=True).stdout


def place(path, data, force):
    """Write an artifact unless an identical one is there; never silently replace."""
    rel = os.path.relpath(path, rc.ROOT)
    if os.path.exists(path):
        with open(path, "rb") as f:
            if f.read() == data:
                print(f"  unchanged  {rel}")
                return
        if not force:
            rc.die(f"{rel} already exists with different contents — a released "
                   f"artifact is immutable.  Bump the version, or pass --force if "
                   f"it was never published.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)
    print(f"  wrote      {rel}  ({len(data)} bytes)")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("variants", nargs="*", metavar="c|rs", help="variants to build (default: both)")
    ap.add_argument("--product", help="override the product taken from the repo name")
    ap.add_argument("--src", action="store_true", help="also pack source tarballs")
    ap.add_argument("--min-from", help="oldest version this release may upgrade FROM (default *)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="build uncommitted working trees (never publish these)")
    ap.add_argument("--gnu", action="store_true",
                    help="also build a dynamic glibc artifact on this host")
    ap.add_argument("--no-smoke", action="store_true", help="skip the start-up smoke tests")
    ap.add_argument("--force", action="store_true", help="replace an existing, different artifact")
    ap.add_argument("--c-src", help="C source tree (default from relcommon.PRODUCTS)")
    ap.add_argument("--rs-src", help="Rust source tree (default from relcommon.PRODUCTS)")
    a = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)   # keep order with stderr notes

    prod = rc.product(a.product)
    bad = [v for v in a.variants if v not in rc.VARIANTS]
    if bad:
        rc.die(f"unknown variant(s) {', '.join(bad)} (c, rs)")
    variants = [v for v in rc.VARIANTS if v in a.variants] or list(rc.VARIANTS)
    if a.min_from and a.min_from != "*" and not rc.VERSION_OK.match(a.min_from.lstrip("v")):
        rc.die(f"--min-from {a.min_from!r} is not a version")
    srcs = {v: os.path.abspath(os.path.expanduser(
        (a.c_src if v == "c" else a.rs_src) or rc.PRODUCTS[prod][v]["src"])) for v in variants}

    # Versions first, all of them, so a mismatch stops before any build.
    vers = {v: read_version(srcs[v], rc.PRODUCTS[prod][v]) for v in variants}
    if len(set(vers.values())) > 1:
        rc.die("the variants disagree on the version: "
               + ", ".join(f"{v}={vers[v]} ({srcs[v]})" for v in variants))
    ver = next(iter(vers.values()))
    if "rs" in variants:
        cargo = open(os.path.join(srcs["rs"], "Cargo.toml")).read()
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', cargo)
        if m and m.group(1) != ver:
            print(f"  note: {srcs['rs']}/Cargo.toml says {m.group(1)}; the daemon reports "
                  f"{ver} (consts.rs), which is what ships", file=sys.stderr)

    arch = platform.machine()
    libcs = ["static"] + (["gnu" if host_libc() == "gnu" else "musl"] if a.gnu else [])
    print(f"{prod} v{ver} — {', '.join(variants)} — {arch}: {', '.join(libcs)}")
    smoke_on = [] if a.no_smoke else ["host"]
    if not a.no_smoke:
        if shutil.which("docker") and have_image(OLD_IMAGE):
            smoke_on.append(OLD_IMAGE)
        else:
            print(f"  note: {OLD_IMAGE} not present — old-machine smoke test skipped",
                  file=sys.stderr)

    for v in variants:
        cfg, src = rc.PRODUCTS[prod][v], srcs[v]
        head, ctime, dirty = git_state(src, a.allow_dirty)
        print(f"[{v}] {src} @ {head[:12]}{' + UNCOMMITTED CHANGES' if dirty else ''}")
        for libc in libcs:
            with tempfile.TemporaryDirectory(prefix=f"mkrel-{prod}-{v}-") as work:
                os.makedirs(os.path.join(work, "src"))
                export_tree(src, dirty, os.path.join(work, "src"))
                binary = build(prod, v, cfg, work, libc, arch)
                if not os.path.isfile(binary):
                    rc.die(f"build finished but {binary} is missing")
                dynamic = elf_interp(binary)
                if libc == "static" and dynamic:
                    rc.die(f"{binary} was meant to be static but asks for a dynamic loader")
                if dynamic:
                    need = glibc_needed(binary)
                    print(f"  {libc:<10} dynamic{f', needs glibc >= ' + '.'.join(map(str, need)) if need else ''}")
                else:
                    print(f"  {libc:<10} fully static")
                # A dynamic build only has to run where it was built.
                for where in (smoke_on if libc == "static" else smoke_on[:1]):
                    smoke(binary, prod, where)
                place(os.path.join(rc.vdir(prod, v), "binaries",
                                   rc.bin_name(prod, v, ver, arch, libc)),
                      pack_binary(prod, binary, ctime), a.force)
        if a.src:
            place(os.path.join(rc.vdir(prod, v), "sources", rc.src_name(prod, v, ver)),
                  pack_source(src, dirty, f"{prod}-{v}-v{ver}", ctime), a.force)

        meta_path = os.path.join(rc.vdir(prod, v), "meta.tsv")
        meta = rc.meta_load(meta_path)
        key = f"v{ver}"
        row = meta.get(key, [rc.today(), cfg["src_deps"], "*"])
        row = [row[0] or rc.today(), row[1] or cfg["src_deps"], row[2] or "*"]
        if a.min_from:
            row[2] = a.min_from
        meta[key] = row
        rc.meta_save(meta_path, meta)
        print(f"  meta.tsv   {key}  date={row[0]} min_from={row[2]}")

    if a.allow_dirty:
        print("\nwarning: built from uncommitted trees — do not publish these artifacts",
              file=sys.stderr)
    print("\nnext: tools/mkmanifest.py, then tools/sign.py <private-key>")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        cmd = " ".join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
        rc.die(f"command failed (exit {e.returncode}): {cmd}")
