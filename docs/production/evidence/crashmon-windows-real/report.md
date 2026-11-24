# Blender crash-monitor report — crashmon-collect-20261124T175656Z-14f9c2

Generated 2026-11-24T17:56:56Z by crashmon 0.1.0 (prototype) · schema `crashmon.bundle/1` · report `crashmon.report/1`

## Verdict

**CRASH**

Bundle kind `collect`, created `2026-11-24T17:56:56Z` on Windows 11.

| Summary | Value |
| --- | --- |
| Crash suspected | yes |
| Exit category | crash |
| Logs | 0 (raw retained: no/tail only) |
| Crash artifacts | 5 found, 1 copied |
| Upload | disabled |

Manifest validation: no schema problems found.

## Run

| Field | Value |
| --- | --- |
| Ended (UTC) | 2026-11-24T17:56:56Z |
| Timeout | none |

Command:

```text
<collected>
```

## Exit status

| Field | Value |
| --- | --- |
| Category | crash |
| Description | Windows status 0xC0000005 (ACCESS_VIOLATION) |
| Raw return code | 3221225477 |
| Windows status | 0xC0000005 |
| Windows exception | ACCESS_VIOLATION |
| Timed out | False |

## Crash artifacts

| Kind | Source | Bundle path | Size | SHA-256 | Text redacted | Note |
| --- | --- | --- | --- | --- | --- | --- |
| minidump | user-crash-dir | crash/user-crash-dir-blender.exe.11652.dmp | 129.4 MiB | 8b1580476ab1... | no | User-supplied crash directory. Binary artifact copied verbatim; contents are not redactable. |
| minidump | windows-crashdumps | not copied | 129.4 MiB | n/a | n/a | Skipped: --max-artifacts (1) reached. |
| minidump | windows-crashdumps | not copied | 128.6 MiB | n/a | n/a | Skipped: --max-artifacts (1) reached. |
| wer-report | windows-wer | not copied | 110.1 KiB | n/a | n/a | Skipped: --max-artifacts (1) reached. |
| wer-report | windows-wer | not copied | 110.1 KiB | n/a | n/a | Skipped: --max-artifacts (1) reached. |

## Log tails

No logs were captured.

## System, GPU, and Blender

| System | Value |
| --- | --- |
| OS | Windows |
| OS release | 11 |
| OS version | 10.0.26200 |
| Platform | Windows-11-10.0.26200-SP0 |
| Architecture | AMD64 |
| CPU | 12th Gen Intel(R) Core(TM) i5-12500H |
| Logical cores | 16 |
| Memory | 15.6 GiB |
| Python | 3.12.14 |

### GPU

| Device | Driver | Vendor | Source |
| --- | --- | --- | --- |
| OrayIddDriver Device | 17.50.19.949 |  | Win32_VideoController |
| Intel(R) Iris(R) Xe Graphics | 31.0.101.5590 |  | Win32_VideoController |

### Blender

| Field | Value |
| --- | --- |
| Probe error | no Blender executable supplied |

## Platform collection capabilities

```json
{
  "system": "Windows",
  "windows": {
    "available": true,
    "crashdumps": {
      "exists": true,
      "path": "<HOME>\\AppData\\Local\\CrashDumps",
      "readable": true,
      "recent_matching_files": 2
    },
    "errors": [],
    "localdumps_registry": {
      "enabled_hint": false,
      "entries": [
        {
          "key": "SOFTWARE\\Microsoft\\Windows\\Windows Error Reporting\\LocalDumps",
          "root": "HKLM",
          "values": {}
        }
      ],
      "errors": [],
      "notes": [
        "LocalDumps values are reported read-only; crashmon never creates or modifies them."
      ],
      "supported": true
    },
    "notes": [
      "Windows local dumps require WER LocalDumps configuration or a WER report; a missing dump is not evidence that no crash occurred.",
      "Crashmon reads WER configuration and directories but never changes them."
    ],
    "supported": true,
    "wer_dirs": [
      {
        "exists": false,
        "label": "local-ReportArchive",
        "path": "<HOME>\\AppData\\Local\\Microsoft\\Windows\\WER\\ReportArchive",
        "readable": false
      },
      {
        "exists": false,
        "label": "local-ReportQueue",
        "path": "<HOME>\\AppData\\Local\\Microsoft\\Windows\\WER\\ReportQueue",
        "readable": false
      },
      {
        "exists": true,
        "label": "system-ReportArchive",
        "path": "C:\\ProgramData\\Microsoft\\Windows\\WER\\ReportArchive",
        "readable": true
      },
      {
        "exists": true,
        "label": "system-ReportQueue",
        "path": "C:\\ProgramData\\Microsoft\\Windows\\WER\\ReportQueue",
        "readable": true
      }
    ],
    "werfault": {
      "exists": true,
      "path": "C:\\WINDOWS\\System32\\WerFault.exe"
    }
  }
}
```

## Environment (allowlist)

Collected 4 allowlisted variable(s); 58 other variable(s) were intentionally not captured.

| Variable | Value |
| --- | --- |
| OS | Windows_NT |
| PROCESSOR_ARCHITECTURE | AMD64 |
| TEMP | <HOME>\AppData\Local\Temp |
| TMP | <HOME>\AppData\Local\Temp |

## Privacy and retention

| Field | Value |
| --- | --- |
| Redaction level | basic |
| Redaction applied | True |
| Replacements | 9 |
| Raw logs included | False |
| Environment scope | allowlist |
| Network | disabled |

Retention: this prototype does not delete bundles automatically. Raw logs are retained unless `--no-raw-logs` was passed. Text is redacted best-effort; binary dumps are copied verbatim and may contain process memory. Delete bundles according to your own data policy.

## Upload boundary

| Field | Value |
| --- | --- |
| Requested | False |
| Consent | False |
| Mode | disabled |
| Status | not-requested |
| Network performed | False |

Uploads are disabled by default. This prototype never opens a network socket; even with --upload-url and --consent it writes only a dry-run upload-plan.json.

## Limitations

- The supervisor observes the direct Blender child process only. Crashes in detached child processes, GPU hang/TDR events, or machine-wide failures may not appear in the bundle.
- A missing crash artifact is not proof that no crash occurred. OS dump policy, permissions, and reporting latency can suppress or delay artifacts.
- Minidumps and cores are not symbolized. Reconstructing stacks needs matching debug symbols (PDB/DWARF) and the exact build ID/commit of the Blender binary.
- Windows WER/LocalDumps does not write local dumps by default; Linux cores depend on core_pattern, ulimit, systemd-coredump/apport, and container permissions; macOS crash reports are written asynchronously.
- Binary dumps/cores are copied verbatim and may contain process memory, paths, and user data. Redaction only covers text logs and text crash reports.
- This prototype never performs network I/O and has no upload backend. A production system needs Crashpad/Sentry/Breakpad, a symbol server, retention controls, and an authenticated intake service.
- Exit codes are interpreted with best-effort platform mappings; shell wrappers may translate signals into 128+N exit codes that cannot be distinguished from ordinary exit statuses.

## Notes

- Exit status supplied manually via --exit-code-hex; it was not observed by the supervisor.
