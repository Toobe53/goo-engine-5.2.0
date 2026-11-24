# Windows installer evidence bundles

Raw Inno Setup logs from the 2026-11-25 Windows 11 / Intel Iris Xe validation.
Logs are gzipped because the plain-text pair is about 2 MB each. The command
lines, result lines and uninstall outcome are summarized in
[`../../evidence-windows-installer.md`](../../evidence-windows-installer.md).

| File | SHA-256 |
| --- | --- |
| `logs/goo-install.log.gz` | `9f72b571a071308a1c98cdc0995cd031ac6988241af681fe858c0f3ea7f37688` |
| `logs/goo-uninstall.log.gz` | `02f0f0cc46497c9be03462d4146688abe3fe0dba9f9d5445db895ad48a7556bc` |
| `logs/goo-update-install.log.gz` | `c74be74ec26ec8178eafcb78ab13f82cf2f5bcf9f44c81e03740834ba8c11f3a` |
| `logs/goo-update-uninstall.log.gz` | `b0fb3102be2291c776b89b57007e348d1dd1b285aecb3f4803b76a95b16407f6` |
| `logs/goo-setup-final-install.log.gz` | `b4442db866121c032718f12e4f716f494f38d715a4e0cf14fd5abb0f5f39c2d4` |
| `logs/goo-setup-final-uninstall.log.gz` | `c122f8b96ee45e6f995add714ecec558a0dd25073ad931cfed5673ef4c33874a` |

The four `goo-*` logs above are the earlier manual-build install and the
archive-update sequence. The two `goo-setup-final-*` logs are the final
wrapper-built artifact, copied to `%TEMP%` and installed into
`C:\goo-final-install-test`.

Key lines:

- `goo-install.log`: `Installation process succeeded.` / `Need to restart Windows? No`;
  uninstall key `...\{6E2F5E3B-7A4C-4D9B-9A1F-2B6C8D0E4F71}_is1`.
- `goo-uninstall.log`: `Uninstallation process succeeded.` / `Removed all? Yes`.
- `goo-update-install.log`: reinstall into `C:\goo-update-test`, exit 0.
- `goo-update-uninstall.log`: final uninstall after update/rollback, exit 0.
- `goo-setup-final-install.log`: final artifact install into
  `C:\goo-final-install-test`, `Installation process succeeded.`
- `goo-setup-final-uninstall.log`: final artifact uninstall,
  `Uninstallation process succeeded.` / `Removed all? Yes`.

Rerun instructions and the writable-log-path requirement are in
[`tools/installer/windows/README.md`](../../../tools/installer/windows/README.md).
