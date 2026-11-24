# Linux GPG signing tool test

This directory records a tooling test, not a production signature. The test key
was generated in a temporary `GNUPGHOME` on the Debian 13 build host, used to
sign a synthetic `SHA256SUMS`, verified with `gpg --verify`, and then deleted
with the temporary directory.

| Item | Value |
| --- | --- |
| Host | Debian 13, `zbq@192.168.1.4` |
| GnuPG | 2.4.7 |
| Script | `tools/release/sign_linux.sh` |
| Test key | `Goo Test <test@example.invalid>`, RSA 2048, 1 day |
| Key fingerprint | `B348D36614E25E87980F4A82AB4BE61470B377F2` |
| Signature file | `SHA256SUMS.asc` |
| Signature SHA-256 | `da7405ddfa278e6781abf3b2cd1b5f6df51304d16eadec275fae09bfc98e3ca0` |

Command shape:

```sh
export GNUPGHOME="$(mktemp -d)/gnupg"
mkdir -m 700 "$GNUPGHOME"
gpg --batch --passphrase '' --quick-gen-key \
  'Goo Test <test@example.invalid>' rsa2048 sign 1d
key=$(gpg --list-secret-keys --with-colons | awk -F: '$1=="sec"{print $5; exit}')
printf '0123...cdef  test.bin\n' > bundle/SHA256SUMS
tools/release/sign_linux.sh bundle --key "$key" --execute
gpg --batch --verify bundle/SHA256SUMS.asc bundle/SHA256SUMS
```

Observed output:

```text
sign_linux.sh: signing with key AB4BE61470B377F2
sign_linux.sh: verified /tmp/goo-sign-test.Qy8Gbf/bundle/SHA256SUMS.asc
sign_linux.sh: sha256 da7405ddfa278e6781abf3b2cd1b5f6df51304d16eadec275fae09bfc98e3ca0
da7405...e3ca0  /tmp/goo-sign-test.Qy8Gbf/bundle/SHA256SUMS.asc
```

The temporary key and directory were removed after the test. This proves the
detached-signature path works; it does **not** make the 5.2.1 release signed,
because no project-controlled key or published fingerprint exists yet.
