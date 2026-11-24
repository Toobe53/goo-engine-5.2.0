# Windows installer

`goo-engine.iss` builds a **per-user**, **unsigned** Inno Setup installer from
the clean `cmake --install` tree. It does not build Blender and it does not
sign the result.

## Build

```powershell
python tools/installer/windows/build_installer.py --version 5.2.1
```

The wrapper is required: Inno Setup sees the source path with a `..\..\..`
prefix, and a long workspace path can exceed the legacy 260-character limit
even when the install tree itself is shorter. The wrapper exposes the source
through a temporary short directory junction (`C:\goo-iw-<id>`) for the
duration of the compile.

Outputs:

- `_work/release-staging/goo-engine-<version>-windows-x64-setup.exe`
- `_work/release-staging/iscc.log`
- size and SHA-256 printed by the wrapper.

`ISCC.exe` is located from `ISCC`, then the per-user Inno Setup 6 install,
then `Program Files`. Build without signing; a real certificate is required
before calling the result signed.

## Install / uninstall

The installer defaults to `%LOCALAPPDATA%\Programs\Goo Engine 5.2` and does
not require administrator rights. Silent install:

```powershell
& .\goo-engine-5.2.1-windows-x64-setup.exe /VERYSILENT /SUPPRESSMSGBOXES `
  /NORESTART /SP- /DIR=C:\goo-install-test /LOG="$env:TEMP\goo-install.log"
```

Use a **writable** log path. Passing `C:\goo-install.log` on this host made
the installer hang during initialization; `%TEMP%` worked.

Uninstall:

```powershell
& C:\goo-install-test\unins000.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART `
  /LOG="$env:TEMP\goo-uninstall.log"
```

## Updates and rollback

Use `tools/release/update.py` with the portable zip for updates. It validates
the archive SHA-256, extracts to a sibling staging directory, backs up the
current tree, swaps it in, and records the state. `rollback` restores the
backup. The installed registry entry and shortcuts keep pointing at the same
directory, so an in-place tree update does not orphan them. The install
directory's parent must be writable for staging and the backup; if it is not,
the state file falls back to `%LOCALAPPDATA%\goo-engine\updates\`.

```powershell
python tools/release/update.py apply --install-dir C:\goo-install-test `
  --package .\goo-engine-5.2.1-windows-x64.zip `
  --expected-sha256 8fea6e94b39aefd37f82f3f193771cf182f94106be8d109eaf0229621ffb93ec

python tools/release/update.py rollback --install-dir C:\goo-install-test
```

The `.exe` installer can also be passed to `apply`; it backs up the current
tree and stages the installer on a short `%TEMP%` path before running it
silently. Its rollback restores the tree but does not revert installer-created
registry entries or shortcuts; the archive path is the recommended update
mechanism. A root-level install (`C:\...`) was used to verify the state-file
fallback and installer-mode apply/rollback.
