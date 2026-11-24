# crashmon supervisor test (controlled native crash)

This directory records a **controlled crash test**, not a product crash. It
exercises `crashmon run` as a supervisor on Windows 11 with the real local
`_install-windows2\blender.exe` (Blender 5.2.1 LTS).

A test-only CPython extension exported `PyInit_crash_ext`, which performed a
null write when imported. Importing it from Blender's bundled Python produced a
native `EXCEPTION_ACCESS_VIOLATION`; unlike `ctypes`, this path is not translated
into a Python `OSError`.

```c
__declspec(dllexport) void *PyInit_crash_ext(void) {
    volatile int *p = (volatile int *)0;
    *p = 1;
    return 0;
}
```

Command (paths shortened here; the manifest stores the command with the home
directory redacted as `<HOME>`):

```powershell
python tools\crashmon\crashmon_cli.py run `
  --blender _install-windows2\blender.exe `
  --output-dir _work\crashmon-supervisor --skip-gpu --skip-probe `
  --timeout 120 --report `
  -- --background --factory-startup --python _work\crashnative\crash_blender_ext.py
```

Result:

| Item | Value |
| --- | --- |
| Bundle | `crashmon-run-20261124T192808Z-ec630c` |
| Verdict | `CRASH` |
| Exit | Windows status `0xC0000005` (`ACCESS_VIOLATION`), raw `3221225477` |
| Duration | 1.015 s |
| Logs | 2 (raw retained) |
| Crash artifacts | 0 found / 0 copied |
| Manifest validation | no schema problems |
| `crashmon` exit | `0` by default; `--fail-on-crash` would return non-zero |

The report explicitly records that no crash artifact was found and that this is
**not** proof that no crash occurred. WER LocalDumps was not registered for this
run, so no `.dmp` was produced; Blender's own crash handler also failed to write
`%TEMP%\blender.crash.txt` with `Permission denied`, but printed a full native
stack to stderr (retained in [stderr.log](stderr.log)).

This verifies the supervisor's exit-code classification, log capture and bundle
finalization. It does **not** replace the real Windows minidump evidence in
[../crashmon-windows-real/README.md](../crashmon-windows-real/README.md), which
proves artifact discovery/copying/hashing. The test DLL/PYD and source were
removed from the tree; no test binary is committed.

Files:

- [report.md](report.md) — generated Markdown report;
- [manifest.json](manifest.json) — generated `crashmon.bundle/1` manifest;
- [stderr.log](stderr.log) — redacted native crash stack.
