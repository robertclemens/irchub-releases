# Signing releases (Ed25519 detached)

The daemon verifies `crypto_ed25519_verify(pub32, releases.txt_bytes, sig64)`
where `releases.sig` is **base64 of the raw 64-byte** signature. `tools/sign.py`
produces exactly that for every manifest in the repo.

## One-time: keep the private key OUT of the repo

```
mkdir -p ~/.keys && chmod 700 ~/.keys
# If you generated the pinned key already, keep using it.  Confirm its PUBLIC
# half matches KEYS.ed25519 / bot.h:
openssl pkey -in ~/.keys/PROJECT-release-ed25519.pem -pubout -outform DER \
  | tail -c 32 | base64 -w0            # must print the KEYS.ed25519 value
chmod 600 ~/.keys/*.pem
```

A passphrase-protected PEM is supported (`sign.py` prompts for it), as is a file
holding the base64 of the raw 32-byte private key.

## Sign

```
tools/sign.py ~/.keys/PROJECT-release-ed25519.pem
```

This signs `releases.txt` and every `<product>/<variant>/releases.txt`. It
refuses a key whose public half is not the one in `KEYS.ed25519` (every node
would reject its signatures), and verifies each signature again before exiting.

## Verify before committing

```
tools/sign.py --verify
```

Reports `ok`, `INVALID` or `MISSING` per manifest and exits non-zero on any
failure. No key needed.

### Without the tools (OpenSSL CLI)

```
openssl pkeyutl -sign -inkey ~/.keys/PROJECT-release-ed25519.pem -rawin \
  -in releases.txt | base64 -w0 > releases.sig
```

(The daemon is the source of truth.)

Never commit `*.pem` private keys. `.gitignore` blocks `*.pem` and `*.key`.
