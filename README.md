# robertclemens/irchub-releases

Signed release channel for **irchub**. Consumed by the irchub self-updater and by
hub_admin-initiated network upgrades.

## Trust model — Ed25519 (NOT GPG)

Releases are authenticated with a raw **Ed25519** detached signature verified via
OpenSSL, fail-closed. There is deliberately **no GPG/PGP `KEYS.asc`** — the pinned
public key lives in `KEYS.ed25519` and matches `irchub/hub.h` `HUB_UPDATE_PUBKEY_B64`
(the same key signs both `irchub-releases` and `irchub-releases`).

* `releases.txt`  — the manifest (plain text, exact bytes are what gets signed)
* `releases.sig`  — base64 of the raw 64-byte Ed25519 signature over `releases.txt`
* `KEYS.ed25519`  — base64 of the 32-byte raw Ed25519 **public** key (verification only)

The **private** key never lives here. Robert signs locally (`tools/sign.py`, see
`SIGNING.md`) and commits the resulting `releases.sig` files.

## Release workflow

```
# 1. In the source trees: bump the version where the daemon reads it —
#      ~/irchub/hub.h            #define HUB_VERSION "X.Y"
#      ~/irchub.rs/src/consts.rs HUB_VERSION ... None => "X.Y"   (+ Cargo.toml version)
#    commit and push both to origin main.

# 2. Build every artifact into this repo (both variants, same version):
tools/mkreleases.py                  # static x86_64 binaries (default)
tools/mkreleases.py --gnu --src      # + a dynamic glibc build and source tarballs
tools/mkreleases.py --min-from 2.0    # this release may only be installed over >= 2.0

# 3. Regenerate every manifest (per-variant + top level) and meta.tsv:
tools/mkmanifest.py

# 4. Sign them all with the release key (refuses any other key):
tools/sign.py ~/.keys/irchub-release-ed25519.pem
tools/sign.py --verify               # re-check at any time, no key needed

# 5. Commit and push this repo.
git add -A && git commit -m "irchub vX.Y" && git push
```

`mkreleases.py` builds the **committed** tree (`git HEAD`) and refuses a tree with
uncommitted changes (`--allow-dirty` exists for throwaway test builds — never
publish those). It refuses to overwrite an existing artifact with different
contents (`--force` only if it was never published): a published artifact's hash
is already in a signed manifest. Both variants must report the same version.

### What gets built

| artifact | built by | runs on |
|---|---|---|
| `…-x86_64-linux-static.tar.gz` (default) | C: Alpine container, static musl + static OpenSSL/libcurl (`tools/musl.Dockerfile` → `ircbuild-musl:static`, built on first use). Rust: this host, `x86_64-unknown-linux-musl` target | any x86_64 Linux — Ubuntu 24.04 / 26.04 LTS (the target servers), older distros, Alpine. Manifest libc = `any` |
| `…-x86_64-linux-gnu.tar.gz` (`--gnu`) | this host, dynamic, distro OpenSSL/libcurl | glibc hosts at least as new as the build host |
| `…-src.tar.gz` (`--src`) | `git archive` of the committed tree | a host with the build deps in `meta.tsv` |

Updaters take the first matching binary row, and the static row is listed first,
so nodes install the static build unless it is missing. Trade-off: a static
binary carries its own OpenSSL/libcurl, so a security fix in those libraries
means cutting a new release rather than an `apt upgrade`.

Every binary is smoke-tested before it is packed: started in an empty directory
it must print its "No config file found" first-run hint — on this host and, when
the image exists, in `ircbuild-rocky8:gcc8`. **Rocky 8 is the old-machine test
environment, not the target platform.**

One-time setup on the build host: Docker (rootless is fine), a Rust toolchain
with `rustup target add x86_64-unknown-linux-musl`, and `python3-cryptography`
for `sign.py`.

## Layout

```
robertclemens/irchub-releases/
├── README.md
├── SIGNING.md
├── KEYS.ed25519
├── releases.txt            # top level: which versions x variants exist
├── releases.sig
├── tools/
│   ├── mkreleases.py       # build artifacts into the tree below
│   ├── mkmanifest.py       # regenerate every releases.txt from what is on disk
│   ├── sign.py             # sign / verify every releases.txt
│   ├── relcommon.py        # shared by the three (source paths, version sources)
│   └── musl.Dockerfile     # static C build image
└── irchub/
    ├── c/                  # C variant
    │   ├── releases.txt    # generated — do not edit
    │   ├── releases.sig
    │   ├── meta.tsv        # version, date, src_deps, min_from (one row per version)
    │   ├── sources/        # irchub-c-vX.Y.Z-src.tar.gz
    │   └── binaries/       # irchub-c-vX.Y.Z-x86_64-linux-{static,gnu}.tar.gz
    └── rs/                 # Rust variant (wire/config compatible with C)
        └── (same)
```

Each binary tarball holds `irchub/irchub`; the updater unpacks it with
`--strip-components=1`. The artifact filename must start with `irchub-` — the
daemon refuses any other product's artifact before downloading it.

The `tools/` directory is byte-identical in `ircbot-releases`; the product is
taken from the repo directory name.

## Manifest columns

See the comment header in any `releases.txt`. The first five are the legacy
`version date url sha256 deps` format (still parsed by older daemons);
`kind arch libc min_from` were appended for binary/variant selection and stepped
upgrades and are ignored by older parsers. `mkmanifest.py` keeps rows whose URL
points outside this repo (e.g. a GitHub tag archive) and drops placeholder rows
without a real SHA-256. Any `releases.sig` whose manifest changed is deleted, so
an unsigned manifest is never left beside a stale signature.
