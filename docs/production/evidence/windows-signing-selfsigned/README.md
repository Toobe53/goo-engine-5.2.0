# Windows self-signed Authenticode demo

Status: **demo only, not production-trusted, not part of the release bundle.**

This records the requested self-signed signing demonstration for the Windows
setup. The production `dist/goo-engine-5.2.1` bundle remains unsigned; the
self-signed copy is a separate artifact under the ignored `_work/` tree.

## Input and output

| Item | Value |
| --- | --- |
| Unsigned setup | `_work/release-staging/goo-engine-5.2.1-windows-x64-setup.exe` |
| Unsigned size | 193,345,800 B |
| Unsigned SHA-256 | `36d7eaaa2d9fe70ce1eb499876a91b5c25742a6147b5914af075b9fe27783d98` |
| Signed demo copy | `_work/signing-demo/goo-engine-5.2.1-windows-x64-setup-selfsigned.exe` |
| Signed size | 193,347,224 B |
| Signed SHA-256 | `da9c900ca5581c402cc22c23ec4090b618cddf6fa9d19780706f92ea5fc8dc7e` |
| Certificate subject | `CN=Goo Engine Test Signing` |
| Certificate SHA-1 | `507F39C2F4D9F58AE9B10D099A08DCD09F134922` |
| Certificate validity | 2026-11-25 to 2026-12-02 (7 days) |
| Timestamp | none |
| Root/TrustedPublisher install | none |

## How it was produced

```powershell
pwsh -NoProfile -File tools/release/sign_windows_selfsigned_demo.ps1 `
  -SetupPath _work/release-staging/goo-engine-5.2.1-windows-x64-setup.exe `
  -OutputDir _work/signing-demo
```

The script creates a temporary self-signed code-signing certificate in
`Cert:\CurrentUser\My`, exports a password-protected PFX, signs only a copy of
the setup with `signtool sign /fd SHA256`, records verification, then deletes
the PFX and the certificate from `CurrentUser\My`. It never writes to `Root` or
`TrustedPublisher`. The `.cer` file in this directory is the public certificate
only.

## Verification result

`verify.txt` is the captured `signtool verify /pa /all /v` output. It reports
the signer and the signed file SHA-256 (`D6AF5FFA...`), then exits `1`:

```text
SignTool Error: A certificate chain processed, but terminated in a root
certificate which is not trusted by the trust provider.
```

`Get-AuthenticodeSignature` reports `UnknownError` and signer
`CN=Goo Engine Test Signing`; it does not report `NotSigned` or
`HashMismatch`. In other words, the Authenticode signature is present and
parseable, but Windows does not trust the self-signed root. That is the expected
result for this demo, not a production signing success.

## What this does not prove

- It does not make the installer trusted on user machines.
- It does not add a timestamp or a publicly trusted chain.
- It is not included in `dist/goo-engine-5.2.1` and does not change the published
  SHA-256 sums.
- Production signing still requires a project-controlled OV/EV certificate
  (and timestamp service), or a separately managed signing identity.
