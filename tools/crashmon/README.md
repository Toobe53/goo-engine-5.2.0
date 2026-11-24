# crashmon — prototype Blender crash supervisor

`crashmon` is a small, standard-library-only crash-monitoring prototype for
Blender / Goo Engine production validation. It launches Blender as a direct
child process, captures stdout/stderr with rotation and redaction, records the
exit status, collects whatever native crash artifacts the operating system
left behind, and writes a self-contained bundle (`manifest.json`, logs, crash
files, optional Markdown report).

**Status: prototype.** The Windows paths were exercised on a real Windows 11
host with Blender 5.2.3 LTS. The Linux and macOS collectors are implemented but
were only exercised through injected/mocked capability probes in this delivery;
see `docs/production/crash-monitoring.md` for the verified/unverified matrix.

The tool performs **no network I/O**. Upload support is an explicit dry-run
planner only.

## Requirements

- Python 3.9+ (Blender's bundled Python works; use
  `<blender>/<version>/python/bin/python.exe` on Windows if the system Python
  is a Microsoft Store stub).
- No third-party packages.
- Blender is only needed for `run` and for `doctor`/auto-detection; `collect`
  and `report` work without it.

## Quick start

From `tools/crashmon`:

```text
python -m crashmon doctor
python -m crashmon run --blender "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --output-dir ./bundles --report
```

Run a real background job (everything after `--` is passed to Blender
unchanged):

```text
python -m crashmon run --blender /path/to/blender --output-dir ./bundles --report -- --background scene.blend --python smoke.py
```

With no arguments after `--`, crashmon uses a short, non-interactive
`--background --factory-startup` startup probe. That is intentional: a test or
CI invocation must not open a GUI.

Collect an existing log and crash directory without launching Blender:

```text
python -m crashmon collect --output-dir ./bundles --log blender.log --crash-dir ./crashes --exit-code-hex 0xC0000005 --report
```

Render a bundle later:

```text
python -m crashmon report ./bundles/crashmon-run-YYYYMMDDTHHMMSSZ-abcdef --format md
python -m crashmon report ./bundles/crashmon-run-YYYYMMDDTHHMMSSZ-abcdef --format json -o report.json
```

From the repository root, `python tools/crashmon/crashmon_cli.py ...` is an
equivalent launcher.

## Commands

| Command | Purpose |
| --- | --- |
| `run` | Start Blender as a direct child, tee/rotate logs, classify exit code/signal, collect crash artifacts, finalize bundle. |
| `collect` | Build a bundle from existing logs and crash directories; optionally record a synthetic exit status. |
| `doctor` | Read-only platform capability report. Exits 0 even when capabilities are missing (`--strict` exits 2 when no artifact source exists). |
| `report` | Render `manifest.json` as Markdown or JSON. Never contacts the network. |

Common options include `--output-dir`, `--name`, `--redact {none,basic,strict}`,
`--skip-gpu`, `--skip-probe`, `--skip-artifacts`, repeatable `--crash-dir`,
`--crashdumps-dir`, `--include-system-cores` (Linux, opt-in), log/artifact size
limits, `--upload-url` + `--consent`, `--json`, `--report`, and
`--fail-on-crash`. See `python -m crashmon <command> --help`.

`run` additionally supports `--timeout`, `--since-minutes` (default 30),
`--tail-lines` (default 80), `--no-raw-logs`, and `--quiet`.

## Bundle layout

```text
<output-dir>/
  crashmon-run-20261124T053453Z-c01919/
    manifest.json          # schema crashmon.bundle/1
    report.md              # only with --report
    upload-plan.json       # only with --upload-url --consent (dry-run)
    logs/
      stdout.log           # rotating, redacted
      stdout.log.1 ...     # backups if rotation happened
      stderr.log
    crash/
      <artifact files>     # copied native dumps/reports
```

The manifest records tool/system/OS/CPU/memory/GPU/Blender build metadata, the
exact argv and working directory, exit code and signal mapping, allowlisted
environment values, log hashes/tails, artifact hashes/sizes/relative paths,
platform collection capabilities, privacy/redaction state, upload boundary, and
limitations. Larger bundles are expected: raw logs are retained by default.

## Privacy and retention

- Default redaction is `basic`: home directory, username, and hostname are
  replaced with `<HOME>`, `<USER>`, `<HOSTNAME>`. `strict` additionally
  redacts email addresses and IPv4 addresses.
- Log files are redacted while they are written. Console echo in human mode is
  not redacted; use `--quiet` or `--json` when capturing terminal output.
- Text crash reports (`.wer`, `.ips`, `.crash`, ...) are redacted when copied.
- **Binary minidumps/cores are copied verbatim** and can contain process
  memory, paths, and user data. Redaction cannot reach inside them.
- Only an allowlisted set of environment variables is stored; the full
  environment is never captured.
- Raw logs are retained unless `--no-raw-logs` is used (then only redacted
  tails are kept).
- Nothing is uploaded. `--upload-url` requires `--consent`, and even then only
  writes a local `upload-plan.json` with intended file names, sizes, and
  hashes. `network_performed` is always `false`.
- crashmon does not delete old bundles. Apply your own retention policy.

## What it cannot do

- It observes only the direct Blender child, not detached children, GPU
  hang/TDR events, or machine-wide failures.
- It does not symbolize dumps or resolve build IDs; that needs matching
  PDB/DWARF/debuginfod and the exact build.
- A missing crash artifact is **not** proof that no crash occurred. Windows
  needs WER LocalDumps/WER reports; Linux needs coredumpctl/core_pattern/ulimit
  and permissions; macOS writes DiagnosticReports asynchronously.
- It is not a production telemetry stack. See
  `docs/production/crash-monitoring.md` for the production gap (Crashpad,
  Sentry, authenticated intake, symbol server, retention, consent UX).

## Tests

From `tools/crashmon`:

```text
python -m py_compile crashmon\__init__.py crashmon\__main__.py crashmon_cli.py crashmon\util.py crashmon\redaction.py crashmon\probes.py crashmon\collectors.py crashmon\bundle.py crashmon\runner.py crashmon\upload.py crashmon\report.py crashmon\cli.py tests\test_crashmon.py
python -m unittest discover -s tests -v
```

Current suite: 44 tests, all passing on Windows 11 with Python 3.13.13. Tests
cover manifest validation, redaction, exit-code mapping, artifact
materialization/hashing, log rotation, timeout handling, report rendering,
upload consent, and CLI `run`/`collect`/`doctor`/`report` end-to-end.

See [`docs/production/crash-monitoring.md`](../../docs/production/crash-monitoring.md)
for the full design, platform matrix, privacy/retention policy, production
recommendations, and the honest list of unverified paths.
