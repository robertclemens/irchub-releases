#!/usr/bin/env python3
"""Sign every manifest of this release repo, or verify the signatures.

    tools/sign.py <private-key>      sign releases.txt + each <variant>/releases.txt
    tools/sign.py --verify           check every releases.sig against KEYS.ed25519

The key may be a PEM file (openssl genpkey -algorithm ed25519, optionally
passphrase-protected — you are prompted) or a file holding the base64 of the
raw 32-byte private key.  It is refused unless its public half is the one in
KEYS.ed25519, which is the key every daemon is compiled with — a signature
from any other key would be rejected by every node in the field.

Each releases.sig is the base64 of the raw 64-byte Ed25519 signature over the
exact bytes of the releases.txt beside it, which is what both updaters verify.
Every signature written is verified again before this exits.
"""
import argparse
import base64
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relcommon as rc  # noqa: E402

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)
except ImportError:
    rc.die("python3 'cryptography' is required (pip install --user cryptography)")


def wipe(buf):
    """Best effort: Python may still hold copies, but not in this buffer."""
    for i in range(len(buf)):
        buf[i] = 0


def load_key(path):
    st = os.stat(path)
    if st.st_mode & 0o077:
        print(f"  warning: {path} is readable by group/others (chmod 600 it)", file=sys.stderr)
    buf = bytearray(open(path, "rb").read())
    try:
        if buf.lstrip().startswith(b"-----BEGIN"):
            try:
                key = serialization.load_pem_private_key(bytes(buf), password=None)
            except TypeError:   # encrypted PEM
                try:
                    pw = bytearray(getpass.getpass(f"passphrase for {path}: ").encode())
                except EOFError:
                    rc.die(f"{path} is passphrase-protected and no passphrase was given")
                try:
                    key = serialization.load_pem_private_key(bytes(buf), password=bytes(pw))
                finally:
                    wipe(pw)
            if not isinstance(key, Ed25519PrivateKey):
                rc.die(f"{path} is not an Ed25519 private key")
            return key
        raw = bytearray(base64.b64decode(bytes(buf).strip(), validate=True))
        try:
            if len(raw) != 32:
                rc.die(f"{path}: expected a PEM key or base64 of a raw 32-byte key")
            return Ed25519PrivateKey.from_private_bytes(bytes(raw))
        finally:
            wipe(raw)
    except ValueError as e:
        rc.die(f"{path}: cannot load key ({e})")
    finally:
        wipe(buf)


def verify_all(prod, pub):
    bad = 0
    for m in rc.manifests(prod):
        rel = os.path.relpath(m, rc.ROOT)
        sig_path = os.path.join(os.path.dirname(m), "releases.sig")
        if not os.path.exists(sig_path):
            print(f"  MISSING    {rel}")
            bad += 1
            continue
        try:
            sig = base64.b64decode(open(sig_path, "rb").read().strip(), validate=True)
            pub.verify(sig, open(m, "rb").read())
            print(f"  ok         {rel}")
        except (InvalidSignature, ValueError):
            print(f"  INVALID    {rel}")
            bad += 1
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("key", nargs="?", help="private key file (PEM or base64 raw)")
    ap.add_argument("--verify", action="store_true", help="only verify existing signatures")
    ap.add_argument("--product", help="override the product taken from the repo name")
    a = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)   # keep order with stderr notes
    if not a.verify and not a.key:
        ap.error("give the private key file, or --verify")

    prod = rc.product(a.product)
    pinned = rc.pinned_pubkey()
    pub = Ed25519PublicKey.from_public_bytes(pinned)

    if not a.verify:
        key = load_key(a.key)
        mine = key.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw)
        if mine != pinned:
            rc.die("this key's public half is not the one in KEYS.ed25519 — "
                   "the daemons would reject every signature it makes")
        for m in rc.manifests(prod):
            if not os.path.exists(m):
                rc.die(f"{os.path.relpath(m, rc.ROOT)} is missing — run tools/mkmanifest.py")
            sig = key.sign(open(m, "rb").read())
            out = os.path.join(os.path.dirname(m), "releases.sig")
            with open(out + ".tmp", "wb") as f:
                f.write(base64.b64encode(sig) + b"\n")
            os.replace(out + ".tmp", out)
            print(f"  signed     {os.path.relpath(m, rc.ROOT)}")
        del key
        print("verifying against KEYS.ed25519:")

    bad = verify_all(prod, pub)
    if bad:
        sys.exit(f"{bad} manifest(s) not validly signed")
    print("all manifests validly signed" + ("" if a.verify else " — commit and push"))


if __name__ == "__main__":
    main()
