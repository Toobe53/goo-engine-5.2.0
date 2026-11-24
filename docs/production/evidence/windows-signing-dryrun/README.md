# Windows signing dry-run check

This directory records a dry-run of `tools/release/sign_windows.ps1` against the
real 5.2.1 setup. It is not a signature and does not claim that the setup is
signed.

The dry-run found a real PowerShell bug: `Resolve-SignTool` returned
`$candidates[0]` when the kit search produced one path. In PowerShell a
one-element pipeline result is a string, so index 0 was the character `C`; the
printed command used `& "C" sign ...`. The script now returns
`@($candidates)[0]`, and this run resolves the full `signtool.exe` path.

| Item | Value |
| --- | --- |
| Host | Windows 11, PowerShell 7 |
| signtool | `C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe` |
| Artifact | `goo-engine-5.2.1-windows-x64-setup.exe` |
| Mode | dry-run, no signing |
| Exit code | `0` |

Command:

```powershell
pwsh -NoProfile -File tools/release/sign_windows.ps1 `
  -ArtifactsDir dist/goo-engine-5.2.1
```

The output is in [dry-run.txt](dry-run.txt). `-Execute` was not run: no
project-controlled code-signing certificate is available. A temporary
self-signed certificate test was attempted, but importing the test root
certificate blocked in a non-interactive shell; the certificate and test files
were removed, and no test root remains trusted.
