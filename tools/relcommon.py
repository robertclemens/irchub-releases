"""Shared by mkreleases.py, mkmanifest.py and sign.py.

The same three scripts live in ircbot-releases/tools and irchub-releases/tools;
the product is taken from the repo's directory name (<product>-releases), so
the files stay byte-identical between the two repos.
"""
import base64
import datetime
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VARIANTS = ("c", "rs")

# Where each daemon actually takes its version from.  The Rust builds report
# the literal in consts.rs, not Cargo.toml's version (IRCBOT_VERSION /
# IRCHUB_VERSION override both at build time; the release build never sets
# them, so the tree's own literal is what ships).
PRODUCTS = {
    "ircbot": {
        "c": {
            "src": "~/ircbot",
            "version_file": "bot.h",
            "version_re": r'#define\s+BOT_VERSION\s+"([^"]+)"',
            "build": "make clean >/dev/null && make USE_CURL=1",
            "build_static": "make clean >/dev/null && make USE_CURL=1 "
                            "LDFLAGS=\"-static $(pkg-config --static --libs libcurl)\"",
            "binary": "ircbot",
            "src_deps": "gcc,make,libssl,libcurl",
        },
        "rs": {
            "src": "~/ircbot.rs",
            "version_file": "src/consts.rs",
            "version_re": r'BOT_VERSION:\s*&str\s*=\s*match\s+option_env!\("IRCBOT_VERSION"\)\s*\{'
                          r'\s*Some\(v\)\s*=>\s*v,\s*None\s*=>\s*"([^"]+)"',
            "cargo_bin": "ircbot",
            "src_deps": "cargo",
        },
    },
    "irchub": {
        "c": {
            "src": "~/irchub",
            "version_file": "hub.h",
            "version_re": r'#define\s+HUB_VERSION\s+"([^"]+)"',
            # `release` is -O2 + hardening; `production` adds -march=native,
            # which would ship a binary tied to the build machine's CPU.
            "build": "make clean >/dev/null && make release",
            "build_static": "make clean >/dev/null && make release "
                            "LIBS=\"-static -lpthread $(pkg-config --static --libs libcurl)\"",
            "binary": "bin/irchub",
            "src_deps": "gcc,make,libssl,libcurl",
        },
        "rs": {
            "src": "~/irchub.rs",
            "version_file": "src/consts.rs",
            "version_re": r'HUB_VERSION:\s*&str\s*=\s*match\s+option_env!\("IRCHUB_VERSION"\)\s*\{'
                          r'\s*Some\(v\)\s*=>\s*v,\s*None\s*=>\s*"([^"]+)"',
            "cargo_bin": "irchub",
            "src_deps": "cargo",
        },
    },
}

VERSION_OK = re.compile(r"^[0-9][0-9A-Za-z.\-]*$")


def die(msg):
    sys.exit(f"error: {msg}")


def product(override=None):
    p = override or os.path.basename(ROOT)
    if p.endswith("-releases"):
        p = p[: -len("-releases")]
    if p not in PRODUCTS:
        die(f"cannot tell the product from {ROOT!r}; pass --product ircbot|irchub")
    return p


# CPU features each variant's artifacts need (manifest column 10), per arch,
# as /proc/cpuinfo names them.  The Rust builds use rustls on graviola, which
# asserts these at its first crypto call — a node without them must be told
# "unable" at PREPARE, not crash mid-upgrade.  Mirrors graviola 0.4.1's
# verify_cpu_features(); the C builds (OpenSSL) need nothing.
CPU_NEEDS = {
    "rs": {
        "x86_64": "aes,pclmulqdq,bmi1,adx,avx,avx2",
        "aarch64": "neon,aes,pmull,sha2",
    },
}


def cpu_needs(variant, arch):
    """Column 10 for one row: '-' = none; a src row (arch 'any') carries every
    arch's set, each entry qualified 'arch:feature'."""
    per = CPU_NEEDS.get(variant)
    if not per:
        return "-"
    if arch == "any":
        return ",".join(f"{a}:{f}" for a, fs in sorted(per.items()) for f in fs.split(","))
    return per.get(arch, "-")


def raw_base(prod):
    """What the daemons compile in as <PRODUCT>_UPDATE_BASE, minus the product dir."""
    return f"https://raw.githubusercontent.com/robertclemens/{prod}-releases/main"


def vdir(prod, variant):
    return os.path.join(ROOT, prod, variant)


# Artifact libc labels.  "static" is a fully static (musl) binary that runs on
# any Linux of that arch, so its manifest row says libc "any" — the updaters
# match "any" against every host.  gnu/musl are dynamic builds for that libc.
LIBC_LABELS = ("static", "gnu", "musl")


def manifest_libc(label):
    return "any" if label == "static" else label


def bin_name(prod, variant, ver, arch, libc):
    return f"{prod}-{variant}-v{ver}-{arch}-linux-{libc}.tar.gz"


def src_name(prod, variant, ver):
    return f"{prod}-{variant}-v{ver}-src.tar.gz"


def today():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def verkey(v):
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v.lstrip("v"))]


# --------------------------------------------------------------- meta.tsv
# version <TAB> date <TAB> src_deps <TAB> min_from.  One row per released
# version; the date is fixed the first time a version is seen so a manifest
# regenerated later does not re-date old releases.
META_HEADER = ("# version\tdate\tsrc_deps\tmin_from  (tab-separated; written by "
               "tools/mkreleases.py, read by tools/mkmanifest.py)\n"
               "# src_deps applies to source rows only; binary rows always carry 'none'.\n")


def meta_load(path):
    rows = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                ver = parts[0]
                rest = (parts[1:] + ["", "", ""])[:3]
                rows[ver] = rest
    return rows


def meta_save(path, rows):
    with open(path + ".tmp", "w") as f:
        f.write(META_HEADER)
        for ver in sorted(rows, key=verkey):
            f.write("\t".join([ver] + rows[ver]) + "\n")
    os.replace(path + ".tmp", path)


# --------------------------------------------------------------- keys
def pinned_pubkey():
    """The 32-byte public key every daemon is compiled with (KEYS.ed25519)."""
    with open(os.path.join(ROOT, "KEYS.ed25519")) as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    if len(lines) != 1:
        die("KEYS.ed25519 must hold exactly one base64 key line")
    raw = base64.b64decode(lines[0])
    if len(raw) != 32:
        die("KEYS.ed25519 is not a 32-byte Ed25519 public key")
    return raw


def manifests(prod):
    """Every manifest a daemon or a reader may fetch, top level first."""
    out = [os.path.join(ROOT, "releases.txt")]
    for v in VARIANTS:
        p = os.path.join(vdir(prod, v), "releases.txt")
        if os.path.exists(p):
            out.append(p)
    return out
