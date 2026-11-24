# Real 5.2.1 local release bundle (Windows + Linux)

This directory preserves the audit material for the real Goo Engine 5.2.1
release bundle produced on the two in-scope platforms. The artifacts themselves
are large and remain under the ignored `dist/` / `_work/` directories; only the
checksum, manifest, verification log and this note are committed.

## Artifacts

| Artifact | Size | SHA-256 |
| --- | ---: | --- |
| `goo-engine-5.2.1-windows-x64.zip` | 273,726,768 B | `8fea6e94b39aefd37f82f3f193771cf182f94106be8d109eaf0229621ffb93ec` |
| `goo-engine-5.2.1-windows-x64-setup.exe` | 193,345,800 B | `36d7eaaa2d9fe70ce1eb499876a91b5c25742a6147b5914af075b9fe27783d98` |
| `goo-engine-5.2.1-linux-x64.tar.gz` | 335,439,115 B | `bf3790719df9926bcaf5364a17053a1fa31c6ce36975181ddd2c58836f2c5a60` |

All three are unsigned (Windows code signing and Linux GPG signing were not
available on this machine). The manifest therefore records
`signing.status = unsigned-placeholder`; `verify_release.py` reports that as the
single expected warning.

## Source and build

- Branch: `production/5.2`
- Source commit recorded in the manifest: `a2639291`
- Build id: `local-20261125c`
- Windows source tree: `_install-windows2` (lite build, `7050/7050` targets)
- Linux source tree: `~/goo-work/goo-install` on the Debian 13 GTX 1660 host
- Bundle command:

```powershell
python tools/release/build_release.py --version 5.2.1 --name "Goo Engine" `
  --channel production --artifacts _work/release-artifacts-5.2.1 `
  --out dist/goo-engine-5.2.1 --commit a2639291 --branch production/5.2 `
  --build-id local-20261125c --builder zbq `
  --platform windows --platform linux --apply
python tools/release/verify_release.py --release-dir dist/goo-engine-5.2.1
```

`verification.txt` is the captured output: 0 failures, 1 warning
(`unsigned-placeholder`), 3 artifacts checked.

## Reproducibility and package smoke

- Windows zip: rebuilt twice with fixed timestamps, sorted paths, fixed
  permissions and `ZIP_DEFLATED` level 9; both builds had the same SHA-256.
- Linux tar: `tar --sort=name --mtime='2026-11-24 00:00:00 UTC' --owner=0
  --group=0 --numeric-owner` plus `gzip -n -9`; a second remote rebuild with the
  same flags produced the same SHA-256 (`REPRO_EXIT=0`).
- Linux archive extracted on Debian 13 and started without `LD_LIBRARY_PATH`:
  `blender --version` = `Blender 5.2.1 LTS`, no `ldd` `not found`. The same tree
  created all 13 Goo nodes, rendered EEVEE 64x64 (3,935 B) and the
  `check_shadow_id` pass (3,935 B) at exit 0.
- Windows installer: silent install/uninstall, registry and Start Menu entry,
  archive-based update and rollback were exercised. Details and logs are in
  [`../windows-installer/`](../windows-installer/README.md).

## What this does not prove

- No Windows code signature and no Linux GPG signature.
- No cross-version upgrade, downgrade, interrupted-update recovery, or
  installer rollback of registry state.
- Driver coverage is still two real rows (`win11-intel`,
  `debian-13-nvidia-gtx1660`), not the full 15 Windows/Linux rows.
- The Linux package runtime was validated headlessly on the GTX 1660; no
  interactive desktop session or long soak was run.
- The final installer was launched from a short `%TEMP%` path; launching it
  directly from the long workspace path hung during initialization on this
  host. The installed binary and logs are otherwise the same artifact.
