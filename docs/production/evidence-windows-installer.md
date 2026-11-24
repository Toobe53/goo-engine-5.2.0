# Windows installer, update and rollback evidence

Date: 2026-11-25 (Asia/Shanghai). Host: Windows 11, Intel Iris Xe,
31.0.101.5590. Build: `production/5.2` at `a2639291`, lite release tree
`_install-windows2` from `build-windows-lite`.

## Artifact

Built with Inno Setup 6.7.3 through the wrapper:

```powershell
python tools/installer/windows/build_installer.py --version 5.2.1
```

| Field | Value |
| --- | --- |
| File | `goo-engine-5.2.1-windows-x64-setup.exe` |
| Size | 193,345,800 B |
| SHA-256 | `36d7eaaa2d9fe70ce1eb499876a91b5c25742a6147b5914af075b9fe27783d98` |
| Signed | no |
| Install scope | per-user (`PrivilegesRequired=lowest`) |
| Default directory | `%LOCALAPPDATA%\Programs\Goo Engine 5.2` |

The wrapper creates a temporary short directory junction for the source tree.
Without it, Inno Setup resolves the script-relative source path with a
`..\..\..` prefix and can exceed the 260-character Windows path limit.

The earlier manual build was 193,345,802 B. The installed payload reports
191,852,607 B inside the installer log; the wrapper build above is the release
candidate.

## Install

Command (using a writable `%TEMP%` log path):

The final wrapper artifact was copied to a short `%TEMP%` path before running
(the same SHA-256 as the release artifact):

```powershell
Copy-Item .\goo-engine-5.2.1-windows-x64-setup.exe "$env:TEMP\goo-setup-final.exe"
& "$env:TEMP\goo-setup-final.exe" /VERYSILENT /SUPPRESSMSGBOXES `
  /NORESTART /SP- /DIR=C:\goo-final-install-test /LOG="$env:TEMP\goo-final2.log"
```

Launching the artifact directly from the long workspace path hung during
initialization on this host; the identical copied binary at a short `%TEMP%`
path installed successfully. The install log records
`Original Setup EXE: ...\Temp\goo-setup-final.exe` and `Installation process
succeeded.`

Result: exit code 0; 5,647 files; `blender.exe --version` = `Blender 5.2.1 LTS`.
Local runtime checks on the installed tree: 13 Goo nodes created, EEVEE 64x64
render, and `check_shadow_id` render, all successful. The Start Menu shortcut
`Goo Engine 5.2.1.lnk` and registry uninstall key
`HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\{6E2F5E3B-7A4C-4D9B-9A1F-2B6C8D0E4F71}_is1`
were present.

`/LOG=C:\goo-install.log` on this host made setup hang during initialization;
`C:\` was not writable for the installer process. The log path must point at a
writable directory. Earlier attempts left `goo-setup.tmp` processes and had to
be cleaned up before retrying.

## Uninstall

```powershell
& C:\goo-final-install-test\unins000.exe /VERYSILENT /SUPPRESSMSGBOXES `
  /NORESTART /LOG="$env:TEMP\goo-final-uninstall.log"
```

Result: exit code 0; `Removed all? Yes`; install directory, uninstall key and
Start Menu shortcut were removed.

## Update and rollback

The installer-installed tree was updated with the portable Windows zip:

```powershell
python tools/release/update.py apply --install-dir C:\goo-update-test `
  --package .\goo-engine-5.2.1-windows-x64.zip `
  --expected-sha256 8fea6e94b39aefd37f82f3f193771cf182f94106be8d109eaf0229621ffb93ec
```

Result: exit 0; backup `C:\goo-update-test.backup-20261124T184728Z`;
`blender.exe --version` = `Blender 5.2.1 LTS`; 5,645 files; uninstall registry
key still present and still points at the same directory.

```powershell
python tools/release/update.py rollback --install-dir C:\goo-update-test
```

Result: exit 0; version 5.2.1; file count restored to 5,647 (the installer
tree includes `unins000.exe`/`unins000.dat`); registry key still present.
A diagnostic `C:\goo-update-test.failed-20261124T184733Z` directory remains by
design and was cleaned up manually after evidence collection.

**Scope:** this is a same-version replay, not a cross-version upgrade. The
archive tree replacement preserves registry/shortcuts, but the test does not
prove a newer-build upgrade, downgrade, interrupted copy recovery, or
installer rollback of registry state. The installer path of `update.py` backs
up and replaces the tree but its rollback does not remove registry entries or
shortcuts; the portable zip is the recommended update path.

The installer (`--package ...setup.exe`) path was also exercised on a tree
installed at `C:\goo-update-test2`, where `C:\` does not permit the state file
next to the tree. `apply` and `rollback` both exited 0 after the state path
fell back to `%LOCALAPPDATA%\goo-engine\updates\`; the tree was restored and
`blender.exe --version` remained 5.2.1. The installer is staged on a short
`%TEMP%` path before launch, so the deep-workspace hang is avoided. The parent
directory must still be writable for staging and backup.

Logs are stored gzipped under [`evidence/windows-installer/logs/`](evidence/windows-installer/logs/)
with a summary in [`evidence/windows-installer/README.md`](evidence/windows-installer/README.md).

## Not verified

- No Authenticode signature; SmartScreen reputation is unknown.
- No cross-version upgrade, no automatic rollback on interrupted update.
- No Windows service/background updater, delta update, or uninstall-on-upgrade
  test from a previous release.
- The installer is per-user only; machine-wide install is not implemented.
