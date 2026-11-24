"""Shared host-side helpers for the Goo test gate wrappers.

This module is imported by plain CPython (never inside Blender) and only uses
the standard library.  It centralises the pieces every Goo CTest entry needs:

* parse ``blender --version`` including the ``version_file`` encoding used by
  the blender-file versioning code,
* probe whether the machine can run a headless EEVEE render (GPU/EGL/Metal),
* create deterministic, safely cleaned output directories below a root,
* run a subprocess with a log file, a timeout and no console window,
* write the auditable result JSON documented in ``README-goo-tests.md``.

Exit codes used by every wrapper:

* ``0``  -- pass (all checks passed),
* ``1``  -- fail (a check failed, a script crashed or output is missing),
* ``77`` -- skip (prerequisite missing; CTest reports it as *Not Run* through
  ``SKIP_RETURN_CODE 77`` and never as a pass).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

RESULT_SCHEMA = "goo-test-result/v1"
SKIP_RETURN_CODE = 77
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# ---------------------------------------------------------------------------
# Time / JSON / process helpers
# ---------------------------------------------------------------------------

def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp with a ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _creation_flags() -> int:
    """Avoid spawning visible console windows on Windows."""
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def write_json_atomic(path: Path, data: object) -> None:
    """Write *data* as JSON, replacing *path* only after a complete write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path):
    """Return the decoded JSON document or ``None`` when unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def tail_string(text: str, limit: int = 1800) -> str:
    text = text or ""
    return text[-limit:]


def tail_file(path: Path, limit: int = 1800) -> str:
    try:
        return tail_string(Path(path).read_text(encoding="utf-8", errors="replace"), limit)
    except OSError:
        return ""


def safe_name(name: str) -> str:
    if not SAFE_NAME_RE.match(name or ""):
        raise ValueError(f"unsafe test/output name for a path component: {name!r}")
    return name


def is_within(child: Path, parent: Path) -> bool:
    """True when *child* resolves inside *parent* (both need not exist)."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Blender version / GPU probe
# ---------------------------------------------------------------------------

def blender_version(blender: Path, timeout: float = 60.0) -> dict:
    """Parse ``blender --version``.

    ``version_file`` is the major*100+minor value stored in ``bmain->versionfile``
    (e.g. 502 for a 5.2 file); patch releases do not change it.  The full
    ``major.minor.patch`` triple is kept separately for display.
    """
    info = {
        "binary": str(blender),
        "available": False,
        "version_text": None,
        "major": None,
        "minor": None,
        "patch": None,
        "version_file": None,
        "build_hash": None,
        "error": None,
    }
    binary = Path(blender)
    if not binary.is_file():
        info["error"] = f"Blender binary not found: {binary}"
        return info
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    text = proc.stdout or ""
    info["returncode"] = proc.returncode
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    info["version_text"] = lines[0] if lines else ""
    match = re.search(r"Blender\s+(\d+)\.(\d+)\.(\d+)", info["version_text"])
    if match is None:
        match = re.search(r"Blender\s+(\d+)\.(\d+)", info["version_text"])
        if match is not None:
            info["major"] = int(match.group(1))
            info["minor"] = int(match.group(2))
            info["patch"] = 0
    else:
        info["major"] = int(match.group(1))
        info["minor"] = int(match.group(2))
        info["patch"] = int(match.group(3))
    hash_match = re.search(r"build hash:\s*([0-9a-fA-F]+)", text)
    if hash_match is not None:
        info["build_hash"] = hash_match.group(1)
    if proc.returncode == 0 and info["major"] is not None:
        info["version_file"] = info["major"] * 100 + info["minor"]
        info["available"] = True
    else:
        info["error"] = f"unexpected --version output or exit code {proc.returncode}"
    return info


def probe_gpu(blender: Path, probe_script: Path, output_json: Path,
              timeout: float = 180.0) -> dict:
    """Run the in-Blender GPU probe and normalise its JSON answer.

    The returned dict always has ``available`` (bool) and ``reason`` (str).
    ``available`` is only true when the probe completed a tiny headless EEVEE
    render; backend strings alone are not considered proof.
    """
    result = {
        "available": False,
        "reason": "not probed",
        "backend": None,
        "renderer": None,
        "vendor": None,
        "device_type": None,
        "returncode": None,
        "log_tail": "",
    }
    binary = Path(blender)
    script = Path(probe_script)
    if not binary.is_file():
        result["reason"] = f"Blender binary not found: {binary}"
        return result
    if not script.is_file():
        result["reason"] = f"GPU probe script not found: {script}"
        return result
    output_json = Path(output_json)
    if output_json.exists():
        try:
            output_json.unlink()
        except OSError:
            pass
    command = [
        str(binary),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(script),
        "--",
        "--output",
        str(output_json),
    ]
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["reason"] = f"GPU probe could not run: {type(exc).__name__}: {exc}"
        return result
    result["returncode"] = proc.returncode
    result["log_tail"] = tail_string(proc.stdout, 1800)
    data = read_json(output_json)
    if data is None:
        result["reason"] = (
            f"GPU probe produced no readable JSON (exit {proc.returncode}); "
            "Blender may not support headless EEVEE on this host"
        )
        return result
    platform = data.get("platform") if isinstance(data.get("platform"), dict) else {}
    result.update(
        {
            "available": bool(data.get("available")),
            "reason": str(data.get("reason") or "no reason reported"),
            "backend": data.get("gpu_backend") or platform.get("backend_type"),
            "renderer": platform.get("renderer"),
            "vendor": platform.get("vendor"),
            "device_type": platform.get("device_type"),
            "probe": data,
        }
    )
    return result


# ---------------------------------------------------------------------------
# Output directories / subprocess execution
# ---------------------------------------------------------------------------

def prepare_work_dir(root: Path, test_name: str) -> tuple[Path, Path]:
    """Create ``<root>/<test_name>/`` and return it plus an empty ``work/`` path.

    ``work/`` is *removed* (only when it resolves inside *root*) but not
    recreated: several Goo scripts require ``--output`` not to exist, while
    ``goo_light_groups.py`` accepts an empty directory.  The result JSON lives
    next to ``work/`` and survives repeated runs.

    Raises ``ValueError``/``RuntimeError`` instead of ever deleting outside the
    configured output root.
    """
    root = Path(root)
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    test_root = root / safe_name(test_name)
    test_root.mkdir(parents=True, exist_ok=True)
    work = test_root / "work"
    if work.exists():
        if not is_within(work, root):
            raise RuntimeError(f"refusing to remove output outside root: {work}")
        shutil.rmtree(work)
    return test_root, work


def run_command(command: list, *, timeout: float, log_path: Path,
                cwd: Path | None = None, env: dict | None = None) -> dict:
    """Run *command* with output redirected to *log_path*.

    Returns a dict with ``command``, ``returncode``, ``timed_out``,
    ``duration_seconds`` and ``log``.  A timeout is reported as returncode 124
    (the same convention used by ``timeout(1)``); an OS error as 126.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    returncode = None
    try:
        with log_path.open("wb") as log_file:
            try:
                proc = subprocess.run(
                    command,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    cwd=str(cwd) if cwd else None,
                    env=env,
                    creationflags=_creation_flags(),
                )
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                returncode = 124
            except OSError as exc:
                returncode = 126
                log_file.write(f"\n[goo_test_util] failed to execute: {exc}\n".encode("utf-8", "replace"))
    except OSError as exc:
        returncode = 126
        try:
            log_path.write_text(f"[goo_test_util] cannot open log: {exc}\n", encoding="utf-8")
        except OSError:
            pass
    return {
        "command": [str(part) for part in command],
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "log": str(log_path),
    }


# ---------------------------------------------------------------------------
# Result report
# ---------------------------------------------------------------------------

def make_report(test: str) -> dict:
    """Create the base result document shared by all Goo wrappers."""
    return {
        "schema_version": RESULT_SCHEMA,
        "test": test,
        "status": "pending",
        "skip_reason": None,
        "started_utc": utc_now(),
        "ended_utc": None,
        "duration_seconds": None,
        "argv": list(sys.argv),
        "cwd": str(Path.cwd()),
        "host": {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "executable": sys.executable,
        },
        "blender": None,
        "gpu": None,
        "checks": [],
        "artifacts": [],
        "limitations": [],
        "notes": [],
        "_started_monotonic": time.monotonic(),
    }


def add_check(report: dict, name: str, ok: bool, detail: str = "") -> None:
    report["checks"].append(
        {"name": name, "status": "pass" if ok else "fail", "detail": detail}
    )


def add_artifact(report: dict, kind: str, path: Path | str, **extra) -> None:
    entry = {"kind": kind, "path": str(path)}
    entry.update(extra)
    report["artifacts"].append(entry)


def add_note(report: dict, message: str) -> None:
    report["notes"].append(message)


def add_limitation(report: dict, message: str) -> None:
    report["limitations"].append(message)


def finish(report: dict, json_path: Path) -> int:
    """Finalise, write the JSON report and return the process exit code."""
    started = report.pop("_started_monotonic", None)
    report["ended_utc"] = utc_now()
    if started is not None:
        report["duration_seconds"] = round(time.monotonic() - started, 3)

    # A pass is only possible when every recorded check passed; guards against a
    # wrapper that forgets to downgrade the status after a failed check.
    if report.get("status") in (None, "pending", "pass"):
        failed = [check for check in report.get("checks", []) if check["status"] == "fail"]
        report["status"] = "fail" if failed else "pass"

    status = report["status"]
    json_path = Path(json_path)
    write_json_atomic(json_path, report)

    test = report.get("test", "?")
    if status == "pass":
        print(f"GOO_TEST_PASS: {test}")
    elif status == "skip":
        print(f"GOO_TEST_SKIP: {test}")
        print(f"GOO_TEST_SKIP_REASON: {report.get('skip_reason') or 'unspecified'}")
    else:
        print(f"GOO_TEST_FAIL: {test}")
        for check in report.get("checks", []):
            if check["status"] == "fail":
                print(f"  CHECK_FAIL: {check['name']}: {check.get('detail') or ''}")
    print(f"GOO_TEST_RESULT: {json_path}")
    return {"pass": 0, "fail": 1, "skip": SKIP_RETURN_CODE}.get(status, 1)


def finish_skip(report: dict, json_path: Path, reason: str) -> int:
    report["status"] = "skip"
    report["skip_reason"] = reason
    return finish(report, json_path)


def finish_fail(report: dict, json_path: Path, reason: str) -> int:
    report["status"] = "fail"
    add_check(report, "wrapper_error", False, reason)
    return finish(report, json_path)
