# Blender crash-monitor report — crashmon-run-20261124T192808Z-ec630c

Generated 2026-11-24T19:28:09Z by crashmon 0.1.0 (prototype) · schema `crashmon.bundle/1` · report `crashmon.report/1`

## Verdict

**CRASH**

Bundle kind `run`, created `2026-11-24T19:28:09Z` on Windows 11.

| Summary | Value |
| --- | --- |
| Crash suspected | yes |
| Exit category | crash |
| Logs | 2 (raw retained: yes) |
| Crash artifacts | 0 found, 0 copied |
| Upload | disabled |

Manifest validation: no schema problems found.

## Run

| Field | Value |
| --- | --- |
| Started (UTC) | 2026-11-24T19:28:08Z |
| Ended (UTC) | 2026-11-24T19:28:09Z |
| Duration | 1.015 s |
| Timeout | 120.0 s |
| Working directory | <HOME>\Documents\deepseek-harness\default-workspace\goo-engine-5.2.0 |

Command:

```text
<HOME>\Documents\deepseek-harness\default-workspace\goo-engine-5.2.0\_install-windows2\blender.exe --background --factory-startup --python <HOME>\Documents\deepseek-harness\default-workspace\goo-engine-5.2.0\_work\crashnative\crash_blender_ext.py
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

No crash artifacts were found. This is **not** proof that no crash occurred; see limitations.

## Log tails

### stdout (retained)

- Bundle file: `logs/stdout.log`
- Size: 347 B · SHA-256: `1f22147c4fb0041b524f9c6254346cd11b1ffabaa08278489980a1869ff6a316`
- Rotation: max 64.0 MiB · backups 3 · rotated: False

```text
Blender 5.2.1 LTS
00:00.062  system.path      | WARNING Could not generate a temp file name for '<HOME>\AppData\Local\Temp\blender_a14420'
00:00.062  system.path      | WARNING Could not generate a temp session subdirectory, falling back to '<HOME>\AppData\Local\Temp\'
importing crash_ext
Writing: <HOME>\AppData\Local\Temp\blender.crash.txt
```

### stderr (retained)

- Bundle file: `logs/stderr.log`
- Size: 20.9 KiB · SHA-256: `4e292e61d250b4f648be47859dacdfb0a23499511445501c01247488f3257ff2`
- Rotation: max 64.0 MiB · backups 3 · rotated: False

```text
0x00007FFD9F1E0000 10.0.26100.9818      ntlanman.dll
0x00007FFD9F020000 10.0.26100.1         davclnt.dll
0x00007FFDCEFE0000 10.0.26100.9818      WINSTA.dll
0x00007FFDC3F20000 10.0.26100.7019      wkscli.dll
0x00007FFDA5850000 10.0.26100.1         cscapi.dll
0x00007FFDCDAE0000 10.0.26100.1882      netutils.dll
0x00007FFDCB610000 10.0.26100.9818      uxtheme.dll
0x00000197B7B70000 3.13.13150.1013      python3.DLL
0x00007FFD50570000 3.13.13150.1013      _ctypes.pyd
0x00007FFD95B70000                      libffi-8.dll
0x00007FFD508C0000 3.13.13150.1013      _bz2.pyd
0x00007FFD4D1D0000 3.13.13150.1013      _lzma.pyd
0x00007FFD429E0000 3.13.13150.1013      unicodedata.pyd
0x00007FFD4F5D0000 3.13.13150.1013      _hashlib.pyd
0x00007FFD12C70000 3.5.6.0              libcrypto-3.dll
0x00007FFDCFB10000 10.0.26100.9818      CRYPT32.dll
0x00007FFD4EC80000 3.13.13150.1013      _socket.pyd
0x00007FFDCDB40000 10.0.26100.9818      IPHLPAPI.DLL
0x00007FFD95050000 3.13.13150.1013      select.pyd
0x00007FFD934A0000 3.13.13150.1013      _multiprocessing.pyd
0x00007FFD93450000 3.13.13150.1013      _wmi.pyd
0x00007FFDC9A70000 7.0.26100.9974       PROPSYS.dll
0x00007FFD4D110000 3.13.13150.1013      _ssl.pyd
0x00007FFD299E0000 3.5.6.0              libssl-3.dll
0x00007FFDCE730000 10.0.26100.9818      mswsock.dll
0x00007FFD425D0000                      backend_c.cp313-win_amd64.pyd
0x00007FFD92E00000 3.13.13150.1013      _queue.pyd
0x00007FFDCEA20000 10.0.26100.9818      CRYPTSP.dll
0x00007FFDCE0D0000 10.0.26100.7705      rsaenh.dll
0x00007FFD90B10000 3.13.13150.1013      _uuid.pyd
0x00007FFD4CBE0000                      crash_ext.pyd

# Python backtrace
  File "<frozen importlib._bootstrap>", line 488 in _call_with_frames_removed
  File "<frozen importlib._bootstrap_external>", line 1317 in create_module
  File "<frozen importlib._bootstrap>", line 813 in module_from_spec
  File "<frozen importlib._bootstrap>", line 921 in _load_unlocked
  File "<frozen importlib._bootstrap>", line 1331 in _find_and_load_unlocked
  File "<frozen importlib._bootstrap>", line 1360 in _find_and_load
  File "<HOME>\Documents\deepseek-harness\default-workspace\goo-engine-5.2.0\_work\crashnative\crash_blender_ext.py", line 7 in <module>
```


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

GPU probe skipped: --skip-gpu

### Blender

| Field | Value |
| --- | --- |
| Executable | <HOME>\Documents\deepseek-harness\default-workspace\goo-engine-5.2.0\_install-windows2\blender.exe |

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
      "recent_matching_files": 0
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
| Replacements | 22 |
| Raw logs included | True |
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

- Supervisor observes only the direct Blender child process.
