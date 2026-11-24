"""Supervised process execution with bounded, rotated, redacted log capture.

The supervisor forwards child output to its own stdout/stderr while writing the
same (redacted) text to bundle logs. It records the raw exit status and a
best-effort platform interpretation (POSIX signal or Windows NTSTATUS).
"""

from __future__ import annotations

import collections
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from . import SCHEMA_VERSION  # noqa: F401  (re-exported context for report consumers)
from .redaction import Redactor
from .util import CrashmonError, decode_bytes, ensure_dir, relpath_posix, sha256_and_size, utc_iso

# Windows NTSTATUS values commonly seen when Blender or a GPU driver fails.
NTSTATUS_MAP: dict[int, tuple[str, str]] = {
    0x80000003: ("BREAKPOINT", "crash"),
    0xC0000005: ("ACCESS_VIOLATION", "crash"),
    0xC0000006: ("IN_PAGE_ERROR", "crash"),
    0xC000001D: ("ILLEGAL_INSTRUCTION", "crash"),
    0xC0000025: ("NONCONTINUABLE_EXCEPTION", "crash"),
    0xC000008C: ("ARRAY_BOUNDS_EXCEEDED", "crash"),
    0xC0000094: ("INTEGER_DIVIDE_BY_ZERO", "crash"),
    0xC0000096: ("PRIVILEGED_INSTRUCTION", "crash"),
    0xC00000FD: ("STACK_OVERFLOW", "crash"),
    0xC0000135: ("DLL_NOT_FOUND", "error"),
    0xC0000139: ("ENTRYPOINT_NOT_FOUND", "error"),
    0xC0000142: ("DLL_INIT_FAILED", "error"),
    0xC000013A: ("CONTROL_C_EXIT", "interrupted"),
    0xC0000374: ("HEAP_CORRUPTION", "crash"),
    0xC0000409: ("STACK_BUFFER_OVERRUN", "crash"),
    0xC0000420: ("ASSERTION_FAILURE", "crash"),
}

CRASH_SIGNALS: dict[int, str] = {}
for _name in ("SIGSEGV", "SIGABRT", "SIGBUS", "SIGILL", "SIGFPE", "SIGSYS", "SIGTRAP"):
    _value = getattr(signal, _name, None)
    if _value is not None:
        CRASH_SIGNALS[int(_value)] = _name

TERMINATION_SIGNALS = {
    int(getattr(signal, "SIGKILL", 9)): "killed",
    int(getattr(signal, "SIGTERM", 15)): "terminated",
    int(getattr(signal, "SIGINT", 2)): "interrupted",
    int(getattr(signal, "SIGHUP", 1)): "terminated",
}


def _unsigned32(value: int) -> int:
    return value & 0xFFFFFFFF if value < 0 else value


def classify_returncode(
    returncode: Optional[int],
    *,
    system: Optional[str] = None,
    timed_out: bool = False,
) -> dict[str, Any]:
    """Map an exit status to a stable crash category.

    Kept platform-independent via the ``system`` argument so both branches are
    unit-testable on any host.
    """
    system = system or platform.system()
    result: dict[str, Any] = {
        "raw": returncode,
        "code_hex": None,
        "signal": None,
        "signal_name": None,
        "windows_exception": None,
        "category": "unknown",
        "description": "no exit status recorded",
        "timed_out": bool(timed_out),
    }
    if timed_out:
        result["category"] = "timeout"
        result["description"] = "process did not exit before the configured timeout and was terminated"
        return result
    if returncode is None:
        result["category"] = "spawn-error"
        result["description"] = "process could not be started"
        return result

    if system == "Windows":
        value = _unsigned32(int(returncode))
        result["code_hex"] = "0x{:08X}".format(value)
        if value == 0:
            result["category"] = "success"
            result["description"] = "exit code 0"
            return result
        if value in NTSTATUS_MAP:
            name, category = NTSTATUS_MAP[value]
            result["windows_exception"] = {"code_hex": result["code_hex"], "name": name}
            result["category"] = category
            result["description"] = "Windows status {} ({})".format(result["code_hex"], name)
            return result
        if value >= 0xC0000000:
            result["windows_exception"] = {"code_hex": result["code_hex"], "name": "UNKNOWN_NTSTATUS"}
            result["category"] = "crash"
            result["description"] = "unrecognized Windows crash status {}".format(result["code_hex"])
            return result
        if value >= 0x80000000:
            result["windows_exception"] = {"code_hex": result["code_hex"], "name": "UNKNOWN_STATUS"}
            result["category"] = "exception"
            result["description"] = "Windows warning/status {}".format(result["code_hex"])
            return result
        result["category"] = "error"
        result["description"] = "exit code {}".format(value)
        return result

    code = int(returncode)
    if code == 0:
        result["category"] = "success"
        result["description"] = "exit code 0"
        return result
    if code < 0:
        number = -code
        result["signal"] = number
        try:
            name = signal.Signals(number).name
        except ValueError:
            name = "SIG{}".format(number)
        result["signal_name"] = name
        if number in CRASH_SIGNALS:
            category = "crash"
            description = "terminated by {} (signal {})".format(name, number)
        elif number in TERMINATION_SIGNALS:
            category = TERMINATION_SIGNALS[number]
            description = "stopped by {} (signal {})".format(name, number)
        else:
            category = "signal"
            description = "terminated by {} (signal {})".format(name, number)
        result["category"] = category
        result["description"] = description
        return result
    result["category"] = "error"
    result["description"] = "exit code {}".format(code)
    return result


class RotatingTextWriter:
    """Write UTF-8 text to rotating files with optional redaction."""

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        max_bytes: int = 0,
        backups: int = 3,
        retain: bool = True,
        redactor: Optional[Redactor] = None,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max(0, int(max_bytes))
        self.backups = max(0, int(backups))
        self.retain = bool(retain)
        self.redactor = redactor
        self.total_bytes = 0
        self.redactions = 0
        self.rotated = False
        self._size = 0
        self._handle = None
        self._lock = threading.Lock()
        ensure_dir(self.path.parent)
        if self.retain:
            self._handle = open(self.path, "wb")

    def write_text(self, text: str) -> str:
        redacted = text
        if self.redactor is not None:
            before = self.redactor.replacements
            redacted = self.redactor.redact(text)
            self.redactions += self.redactor.replacements - before
        encoded = redacted.encode("utf-8", errors="replace")
        with self._lock:
            self.total_bytes += len(encoded)
            if not self.retain or self._handle is None:
                return redacted
            if self.max_bytes and self._size > 0 and self._size + len(encoded) > self.max_bytes:
                self._rotate_locked()
            self._handle.write(encoded)
            self._handle.flush()
            self._size += len(encoded)
        return redacted

    def _rotate_locked(self) -> None:
        self._handle.close()
        self.rotated = True
        if self.backups > 0:
            oldest = Path("{}.{}".format(self.path, self.backups))
            try:
                if oldest.exists():
                    oldest.unlink()
            except OSError:
                pass
            for index in range(self.backups - 1, 0, -1):
                source = Path("{}.{}".format(self.path, index))
                destination = Path("{}.{}".format(self.path, index + 1))
                if source.exists():
                    try:
                        os.replace(source, destination)
                    except OSError:
                        pass
            if self.path.exists():
                try:
                    os.replace(self.path, "{}.1".format(self.path))
                except OSError:
                    pass
        self._handle = open(self.path, "wb")
        self._size = 0

    def files(self) -> list[Path]:
        files: list[Path] = []
        if self.path.exists():
            files.append(self.path)
        for index in range(1, self.backups + 1):
            candidate = Path("{}.{}".format(self.path, index))
            if candidate.exists():
                files.append(candidate)
        return files

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.close()
                finally:
                    self._handle = None


class _StreamCapture:
    def __init__(
        self,
        *,
        name: str,
        pipe: Any,
        writer: RotatingTextWriter,
        tail_lines: int,
        echo_stream: Any,
        echo: bool,
    ) -> None:
        self.name = name
        self.pipe = pipe
        self.writer = writer
        self.tail: collections.deque[str] = collections.deque(maxlen=max(0, tail_lines))
        self.echo_stream = echo_stream
        self.echo = echo
        self.errors: list[str] = []
        self.thread = threading.Thread(target=self._run, name="crashmon-{}".format(name), daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float = 5.0) -> None:
        self.thread.join(timeout=timeout)

    def _run(self) -> None:
        try:
            while True:
                chunk = self.pipe.readline()
                if not chunk:
                    break
                text = decode_bytes(chunk)
                redacted = self.writer.write_text(text)
                self.tail.append(redacted.rstrip("\r\n"))
                if self.echo and self.echo_stream is not None:
                    _echo_bytes(self.echo_stream, chunk)
        except (OSError, ValueError) as exc:  # pipe closed during shutdown
            self.errors.append("{} reader: {}".format(self.name, exc))
        finally:
            try:
                self.pipe.close()
            except (OSError, ValueError):
                pass


def _echo_bytes(stream: Any, chunk: bytes) -> None:
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(chunk)
            buffer.flush()
            return
        except (OSError, ValueError, UnicodeError):
            pass
    try:
        stream.write(decode_bytes(chunk))
        stream.flush()
    except (OSError, ValueError, UnicodeError):
        pass


def _log_entry(capture: _StreamCapture, bundle: Path, *, retain: bool) -> dict[str, Any]:
    writer = capture.writer
    tail_list = list(capture.tail)
    if retain:
        files = writer.files()
        current = files[0] if files else writer.path
        file_records = []
        for file_path in files:
            if not file_path.exists():
                continue
            digest, size = sha256_and_size(file_path)
            file_records.append(
                {
                    "relative_path": relpath_posix(file_path, bundle),
                    "size_bytes": size,
                    "sha256": digest,
                }
            )
        digest, size = sha256_and_size(current) if current.exists() else ("", 0)
        return {
            "stream": capture.name,
            "relative_path": relpath_posix(current, bundle) if current.exists() else None,
            "size_bytes": size,
            "sha256": digest or None,
            "retained": True,
            "tail_only": False,
            "tail": tail_list,
            "files": file_records,
            "rotation": {
                "max_bytes": writer.max_bytes,
                "backups": writer.backups,
                "rotated": writer.rotated,
                "rotated_file_count": max(0, len(file_records) - 1),
            },
            "redactions": writer.redactions,
            "errors": list(capture.errors),
        }
    # Tail-only mode: write the (already redacted) deque to one small file.
    tail_path = bundle / "logs" / "{}.tail.txt".format(capture.name)
    ensure_dir(tail_path.parent)
    text = "\n".join(tail_list) + ("\n" if tail_list else "")
    tail_path.write_text(text, encoding="utf-8")
    digest, size = sha256_and_size(tail_path)
    return {
        "stream": capture.name,
        "relative_path": relpath_posix(tail_path, bundle),
        "size_bytes": size,
        "sha256": digest,
        "retained": False,
        "tail_only": True,
        "tail": tail_list,
        "files": [
            {
                "relative_path": relpath_posix(tail_path, bundle),
                "size_bytes": size,
                "sha256": digest,
            }
        ],
        "rotation": {
            "max_bytes": writer.max_bytes,
            "backups": writer.backups,
            "rotated": False,
            "rotated_file_count": 0,
        },
        "redactions": writer.redactions,
        "errors": list(capture.errors),
    }


def run_supervised(
    argv: Sequence[str],
    bundle_dir: os.PathLike[str] | str,
    *,
    redactor: Redactor,
    retain_logs: bool = True,
    max_log_bytes: int = 64 * 1024 * 1024,
    log_backups: int = 3,
    tail_lines: int = 80,
    timeout: float = 0.0,
    echo: bool = True,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[os.PathLike[str] | str] = None,
) -> dict[str, Any]:
    """Run ``argv`` as a direct child, capturing and rotating stdout/stderr."""
    command = [str(part) for part in argv]
    if not command:
        raise CrashmonError("run_supervised requires a non-empty command")
    bundle = Path(bundle_dir)
    logs_dir = ensure_dir(bundle / "logs")
    started = time.monotonic()
    started_dt = datetime.now(timezone.utc)
    result: dict[str, Any] = {
        "started_utc": utc_iso(started_dt),
        "ended_utc": None,
        "duration_s": None,
        "timeout_seconds": timeout if timeout and timeout > 0 else None,
        "timed_out": False,
        "command": command,
        "cwd": str(cwd) if cwd else str(Path.cwd()),
        "exit": classify_returncode(None),
        "logs": [],
        "spawn_error": None,
    }

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env=env,
        )
    except OSError as exc:
        result["spawn_error"] = "{}: {}".format(type(exc).__name__, exc)
        result["exit"] = {
            "raw": None,
            "code_hex": None,
            "signal": None,
            "signal_name": None,
            "windows_exception": None,
            "category": "spawn-error",
            "description": result["spawn_error"],
            "timed_out": False,
        }
        result["ended_utc"] = utc_iso()
        result["duration_s"] = round(time.monotonic() - started, 3)
        return result

    stdout_writer = RotatingTextWriter(
        logs_dir / "stdout.log",
        max_bytes=max_log_bytes,
        backups=log_backups,
        retain=retain_logs,
        redactor=redactor,
    )
    stderr_writer = RotatingTextWriter(
        logs_dir / "stderr.log",
        max_bytes=max_log_bytes,
        backups=log_backups,
        retain=retain_logs,
        redactor=redactor,
    )
    stdout_capture = _StreamCapture(
        name="stdout",
        pipe=process.stdout,
        writer=stdout_writer,
        tail_lines=tail_lines,
        echo_stream=sys.stdout,
        echo=echo,
    )
    stderr_capture = _StreamCapture(
        name="stderr",
        pipe=process.stderr,
        writer=stderr_writer,
        tail_lines=tail_lines,
        echo_stream=sys.stderr,
        echo=echo,
    )
    stdout_capture.start()
    stderr_capture.start()

    try:
        if timeout and timeout > 0:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                result["timed_out"] = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        else:
            process.wait()
    finally:
        stdout_capture.join()
        stderr_capture.join()
        stdout_writer.close()
        stderr_writer.close()
        for pipe in (process.stdout, process.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except (OSError, ValueError):
                pass

    exit_info = classify_returncode(process.returncode, timed_out=result["timed_out"])
    result["exit"] = exit_info
    result["logs"] = [
        _log_entry(stdout_capture, bundle, retain=retain_logs),
        _log_entry(stderr_capture, bundle, retain=retain_logs),
    ]
    result["ended_utc"] = utc_iso()
    result["duration_s"] = round(time.monotonic() - started, 3)
    return result
