#!/usr/bin/env python3
"""Build this product's release artifacts into the repo's layout.

    tools/mkreleases.py [c] [rs] [static] [gnu] [src] [--version VER|all ...]
                        [--commit REF] [--min-from VER] [--allow-dirty]
                        [--force] [--no-smoke] [--c-src DIR] [--rs-src DIR]

The words pick what to build: variants c / rs and artifact kinds static /
gnu / src.  Naming none of a group means all of it, so the bare command
builds every kind of both variants; `rs static` builds only that one target.
(--gnu / --src are old spellings of the gnu / src words.)

Without --version it builds the COMMITTED tree (git HEAD of each source
repo), at the version that tree reports.  --version VER rebuilds a past
release from the source repo's v<VER> tag (or, without a tag, the commit
recorded in meta.tsv, or --commit) and only fills in artifacts that are
missing: an existing one is left alone.  --version all does that for every
version in the variants' meta.tsv; a version that cannot be built (no tag,
an old tree without the static target, a failed smoke test) is reported and
the rest go on.

For each variant it packs the binary as <product>/<product> inside
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

gnu is a dynamic glibc artifact built on THIS host (…-linux-gnu.tar.gz).
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
import time

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
def read_version(src, cfg, ref=None):
    """The version the daemon reports, from the work tree or from commit ref."""
    path = os.path.join(src, cfg["version_file"])
    if ref:
        text = subprocess.run(["git", "-C", src, "show", f"{ref}:{cfg['version_file']}"],
                              capture_output=True, text=True).stdout
        path = f"{path} @ {ref[:12]}"
    else:
        with open(path) as f:
            text = f.read()
    m = re.search(cfg["version_re"], text)
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


def resolve_release(src, ver, meta_commit, commit_opt):
    """(commit, commit_time) a past release is rebuilt from, or None: the
    v<ver> tag, else meta.tsv's recorded commit, else --commit."""
    for ref in (f"v{ver}", meta_commit if meta_commit not in ("", "-") else None, commit_opt):
        if not ref:
            continue
        r = subprocess.run(["git", "-C", src, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            head = r.stdout.strip()
            ctime = int(out(["git", "-C", src, "log", "-1", "--format=%ct", head]).strip())
            return head, ctime
    return None


def tree_files(src, dirty, untracked=True):
    """Files of the release: HEAD's, or with --allow-dirty the working tree's —
    tracked plus untracked-but-not-ignored, so a new file not yet `git add`ed
    is built too (a committed release would miss it: add it before committing)."""
    if dirty:
        names = out(["git", "-C", src, "ls-files", "-z", "--cached"]
                    + (["--others", "--exclude-standard"] if untracked else [])).split("\0")
        return [n for n in names if n and os.path.lexists(os.path.join(src, n))]
    return None   # use git archive


def export_tree(src, dirty, dest, ref="HEAD"):
    """The exact tree the release is built from, into dest."""
    if not dirty:
        p1 = subprocess.Popen(["git", "-C", src, "archive", "--format=tar", ref],
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


def static_libs(variant):
    """The third-party code a static artifact of this variant links in, for
    meta.tsv: the C build's Alpine release and package versions (read from the
    image it was just built in), the Rust build's rustc (its musl target
    carries rustc's own self-contained musl; the crates are in Cargo.lock)."""
    if variant == "c":
        r = subprocess.run(["docker", "run", "--rm", "--network", "none", MUSL_IMAGE, "sh", "-c",
                            "cat /etc/alpine-release; apk list -I 2>/dev/null"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            rc.die(f"cannot list the packages of {MUSL_IMAGE}: {r.stderr.strip()}")
        lines = r.stdout.splitlines()
        got = {}
        for line in lines[1:]:
            m = re.match(r"^\S+?-(\d[^-\s]*-r\d+) \S+ \{([^}]+)\}", line)
            if m and m.group(2) in rc.STATIC_ORIGINS:
                got[m.group(2)] = m.group(1)
        parts = [f"alpine={lines[0].strip()}"] + [f"{k}={v}" for k, v in sorted(got.items())]
    else:
        v = subprocess.run(["rustc", "-V"], capture_output=True, text=True).stdout.split()
        parts = [f"rustc={v[1] if len(v) > 1 else '?'}"]
    field = ",".join(parts)
    if not re.fullmatch(r"[A-Za-z0-9=.,_+~-]+", field):
        rc.die(f"unexpected characters in static_libs {field!r}")
    return field


def musl_target(arch):
    t = f"{arch}-unknown-linux-musl"
    libdir = subprocess.run(["rustc", "--print", "target-libdir", "--target", t],
                            capture_output=True, text=True).stdout.strip()
    if not libdir or not os.path.isdir(libdir):
        rc.die(f"the Rust {t} target is not installed — run: rustup target add {t}")
    return t


# The sandbox env overrides a testnet build compiles in (-DIRC_TEST_HOOKS /
# --features test-hooks).  A binary that reads them could be pointed at
# anyone's release tree and signing key by whoever sets its environment, so a
# release carrying one of these names is refused outright.
TEST_HOOK_VARS = (b"IRCBOT_UPDATE_BASE", b"IRCBOT_UPDATE_PUBKEY",
                  b"IRCHUB_UPDATE_BASE", b"IRCHUB_UPDATE_PUBKEY",
                  b"IRCHUB_BOT_RELEASE_BASE")


def refuse_test_hooks(binary):
    with open(binary, "rb") as f:
        data = f.read()
    hit = [v.decode() for v in TEST_HOOK_VARS if v in data]
    if hit:
        rc.die(f"{binary} was built with test hooks (reads {', '.join(hit)}); "
               "a release must never carry them")


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
        refuse_test_hooks(binary)
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
    refuse_test_hooks(binary)
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


def pack_source(src, dirty, prefix, mtime, ref="HEAD"):
    if not dirty:
        return out_bytes(["git", "-C", src, "archive", "--format=tar.gz",
                          f"--prefix={prefix}/", ref])
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
KINDS = ("static", "gnu", "src")


def build_variant(prod, v, src, ver, ref, ctime, dirty, kinds, arch, smoke_on, a, fill):
    """Build the chosen kinds of one variant at one version into the repo.
    fill: leave an artifact that already exists alone (past releases)."""
    cfg = rc.PRODUCTS[prod][v]
    libcs = (["static"] if "static" in kinds else []) + \
            ([("gnu" if host_libc() == "gnu" else "musl")] if "gnu" in kinds else [])
    libs = None
    for libc in libcs:
        dest = os.path.join(rc.vdir(prod, v), "binaries", rc.bin_name(prod, v, ver, arch, libc))
        if fill and os.path.exists(dest) and not a.force:
            print(f"  present    {os.path.relpath(dest, rc.ROOT)}")
            continue
        with tempfile.TemporaryDirectory(prefix=f"mkrel-{prod}-{v}-") as work:
            os.makedirs(os.path.join(work, "src"))
            export_tree(src, dirty, os.path.join(work, "src"), ref)
            binary = build(prod, v, cfg, work, libc, arch)
            if libc == "static":
                libs = static_libs(v)
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
            place(dest, pack_binary(prod, binary, ctime), a.force)
    if "src" in kinds:
        dest = os.path.join(rc.vdir(prod, v), "sources", rc.src_name(prod, v, ver))
        if fill and os.path.exists(dest) and not a.force:
            print(f"  present    {os.path.relpath(dest, rc.ROOT)}")
        else:
            place(dest, pack_source(src, dirty, f"{prod}-{v}-v{ver}", ctime, ref), a.force)

    meta_path = os.path.join(rc.vdir(prod, v), "meta.tsv")
    meta = rc.meta_load(meta_path)
    key = f"v{ver}"
    first = time.strftime("%Y-%m-%d", time.gmtime(ctime)) if fill else rc.today()
    row = meta.get(key, [first, cfg["src_deps"], "*", "-", "-"])
    row = [row[0] or first, row[1] or cfg["src_deps"], row[2] or "*",
           libs or row[3] or "-", (row[4] or "-") if dirty else ref]
    if a.min_from:
        row[2] = a.min_from
    floored = rc.min_from_floor(ver, row[2])
    if floored != row[2]:
        print(f"  note: min_from {row[2]} -> {floored} (mandatory step, relcommon.MIN_FROM_FLOORS)")
        row[2] = floored
    meta[key] = row
    rc.meta_save(meta_path, meta)
    print(f"  meta.tsv   {key}  date={row[0]} min_from={row[2]} static_libs={row[3]} "
          f"commit={row[4][:12]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", nargs="*", metavar="c|rs|static|gnu|src",
                    help="variants and artifact kinds to build (default: all)")
    ap.add_argument("--version", action="append", default=[], metavar="VER|all",
                    help="rebuild missing artifacts of a past release (repeatable; "
                         "'all' = every version in meta.tsv)")
    ap.add_argument("--commit", help="with one --version and one variant: the commit to "
                                     "build when the source repo has no v<VER> tag")
    ap.add_argument("--product", help="override the product taken from the repo name")
    ap.add_argument("--src", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--gnu", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--min-from", help="oldest version this release may upgrade FROM (default *)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="build uncommitted working trees (never publish these)")
    ap.add_argument("--no-smoke", action="store_true", help="skip the start-up smoke tests")
    ap.add_argument("--force", action="store_true", help="replace an existing, different artifact")
    ap.add_argument("--c-src", help="C source tree (default from relcommon.PRODUCTS)")
    ap.add_argument("--rs-src", help="Rust source tree (default from relcommon.PRODUCTS)")
    a = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)   # keep order with stderr notes

    prod = rc.product(a.product)
    bad = [w for w in a.what if w not in rc.VARIANTS + KINDS]
    if bad:
        rc.die(f"unknown word(s) {', '.join(bad)} (c, rs, static, gnu, src)")
    variants = [v for v in rc.VARIANTS if v in a.what] or list(rc.VARIANTS)
    kinds = [k for k in KINDS if k in a.what or (k == "gnu" and a.gnu) or (k == "src" and a.src)]
    if not any(k in a.what for k in KINDS):
        kinds = list(KINDS)
    if a.min_from and a.min_from != "*" and not rc.VERSION_OK.match(a.min_from.lstrip("v")):
        rc.die(f"--min-from {a.min_from!r} is not a version")
    if a.commit and (len(a.version) != 1 or a.version == ["all"] or len(variants) != 1):
        rc.die("--commit needs exactly one --version VER and one variant (c or rs)")
    if a.version and (a.allow_dirty or a.min_from):
        rc.die("--version builds a past release from git as it was released; "
               "--allow-dirty and --min-from do not apply")
    srcs = {v: os.path.abspath(os.path.expanduser(
        (a.c_src if v == "c" else a.rs_src) or rc.PRODUCTS[prod][v]["src"])) for v in variants}

    arch = platform.machine()
    smoke_on = [] if a.no_smoke else ["host"]
    if not a.no_smoke:
        if shutil.which("docker") and have_image(OLD_IMAGE):
            smoke_on.append(OLD_IMAGE)
        else:
            print(f"  note: {OLD_IMAGE} not present — old-machine smoke test skipped",
                  file=sys.stderr)

    if a.version:
        return past_releases(prod, variants, kinds, srcs, arch, smoke_on, a)

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

    print(f"{prod} v{ver} — {', '.join(variants)} — {arch}: {', '.join(kinds)}")
    for v in variants:
        head, ctime, dirty = git_state(srcs[v], a.allow_dirty)
        print(f"[{v}] {srcs[v]} @ {head[:12]}{' + UNCOMMITTED CHANGES' if dirty else ''}")
        build_variant(prod, v, srcs[v], ver, head, ctime, dirty, kinds, arch, smoke_on, a,
                      fill=False)

    if a.allow_dirty:
        print("\nwarning: built from uncommitted trees — do not publish these artifacts",
              file=sys.stderr)
    print("\nnext: tools/mkmanifest.py, then tools/sign.py <private-key>")


def past_releases(prod, variants, kinds, srcs, arch, smoke_on, a):
    """--version: fill in the missing artifacts of releases already made."""
    metas = {v: rc.meta_load(os.path.join(rc.vdir(prod, v), "meta.tsv")) for v in variants}
    if "all" in a.version:
        wanted = sorted({k.lstrip("v") for m in metas.values() for k in m}, key=rc.verkey)
    else:
        wanted = []
        for w in a.version:
            w = w.lstrip("v")
            if not rc.VERSION_OK.match(w):
                rc.die(f"--version {w!r} is not a version")
            if w not in wanted:
                wanted.append(w)
    print(f"{prod} past releases {', '.join(wanted)} — {', '.join(variants)} — "
          f"{arch}: {', '.join(kinds)}")
    failed = []
    for ver in wanted:
        for v in variants:
            key = f"v{ver}"
            if "all" in a.version and key not in metas[v]:
                continue
            row = metas[v].get(key, [""] * rc.META_FIELDS)
            got = resolve_release(srcs[v], ver, row[4], a.commit)
            if not got:
                print(f"[{v}] v{ver}: no v{ver} tag in {srcs[v]} and no commit recorded "
                      f"— skipped (tag it, or pass --commit)", file=sys.stderr)
                failed.append(f"{v} v{ver} (no tag)")
                continue
            head, ctime = got
            print(f"[{v}] v{ver} {srcs[v]} @ {head[:12]}")
            try:
                have = read_version(srcs[v], rc.PRODUCTS[prod][v], head)
                if have != ver:
                    rc.die(f"{head[:12]} reports version {have}, not {ver}")
                build_variant(prod, v, srcs[v], ver, head, ctime, False, kinds, arch,
                              smoke_on, a, fill=True)
            except (SystemExit, subprocess.CalledProcessError) as e:
                why = e.code if isinstance(e, SystemExit) else \
                    f"command failed (exit {e.returncode})"
                print(f"  FAILED     {v} v{ver}: {why}", file=sys.stderr)
                failed.append(f"{v} v{ver}")
    if failed:
        print(f"\nnot built: {', '.join(failed)}", file=sys.stderr)
    print("\nnext: tools/mkmanifest.py, then tools/sign.py <private-key>")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        cmd = " ".join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
        rc.die(f"command failed (exit {e.returncode}): {cmd}")
