"""Small, dependency-free helpers shared by the crashmon prototype.

Everything here is intentionally conservative: external commands are bounded by
timeouts, outputs are capped, and failures are returned as data instead of being
allowed to crash the supervisor.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


class CrashmonError(RuntimeError):
    """An expected, user-facing crashmon failure (bad input, missing bundle...)."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(when: Optional[datetime] = None) -> str:
    value = when or utc_now()
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_iso(when: Optional[datetime] = None) -> str:
    value = when or datetime.now().astimezone()
    return value.astimezone().isoformat(timespec="seconds")


def timezone_name() -> str:
    try:
        return datetime.now().astimezone().tzname() or "unknown"
    except Exception:  # pragma: no cover - platform specific
        return "unknown"


def new_bundle_id() -> str:
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return "crashmon-{}-{}".format(stamp, secrets.token_hex(3))


def decode_bytes(data: bytes, encoding: str = "utf-8") -> str:
    """Decode a byte stream for logs. Invalid bytes are replaced, not fatal."""
    if not data:
        return ""
    return data.decode(encoding, errors="replace")


def decode_maybe_utf16(data: bytes) -> str:
    """Decode small text files that may be UTF-16 (WER Report.wer) or UTF-8."""
    if not data:
        return ""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def sha256_file(path: os.PathLike[str] | str, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_and_size(path: os.PathLike[str] | str) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), total


def human_bytes(value: Optional[int]) -> str:
    if value is None:
        return "unknown"
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024.0 or unit == "TiB":
            if unit == "B":
                return "{:.0f} {}".format(number, unit)
            return "{:.1f} {}".format(number, unit)
        number /= 1024.0
    return "{:.1f} TiB".format(number)


def ensure_dir(path: os.PathLike[str] | str) -> Path:
    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def sanitize_filename(name: str, fallback: str = "artifact") -> str:
    """Keep a single, safe path component for bundle-local files."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    cleaned = cleaned[:120] or fallback
    return cleaned


def unique_path(directory: Path, filename: str) -> Path:
    """Return a non-existing destination inside *directory*."""
    ensure_dir(directory)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 1000):
        candidate = directory / "{}-{}{}".format(stem, index, suffix)
        if not candidate.exists():
            return candidate
    return directory / "{}-{}{}".format(stem, secrets.token_hex(4), suffix)


def atomic_write_text(path: os.PathLike[str] | str, text: str, encoding: str = "utf-8") -> None:
    destination = Path(path)
    ensure_dir(destination.parent)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding=encoding,
        newline="\n",
        dir=str(destination.parent),
        prefix=destination.name + ".",
        suffix=".tmp",
        delete=False,
    )
    temp_name = handle.name
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: os.PathLike[str] | str, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


def read_json(path: os.PathLike[str] | str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_text_limited(path: os.PathLike[str] | str, limit: int = 65536) -> str:
    try:
        with open(path, "rb") as handle:
            return decode_maybe_utf16(handle.read(limit))
    except OSError:
        return ""


def relpath_posix(path: os.PathLike[str] | str, base: os.PathLike[str] | str) -> str:
    try:
        relative = Path(path).resolve().relative_to(Path(base).resolve())
        return relative.as_posix()
    except (ValueError, OSError):
        try:
            return Path(os.path.relpath(str(path), str(base))).as_posix()
        except (ValueError, OSError):
            return Path(path).name


def path_is_within(path: os.PathLike[str] | str, base: os.PathLike[str] | str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def get_username() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - exotic environments
        for key in ("USERNAME", "USER", "LOGNAME"):
            value = os.environ.get(key)
            if value:
                return value
    return ""


def get_hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover
        return ""


def which(command: str) -> Optional[str]:
    return shutil.which(command)


def run_command(
    argv: Sequence[str],
    *,
    timeout: float = 20.0,
    cwd: Optional[os.PathLike[str] | str] = None,
    max_output: int = 200_000,
) -> dict[str, Any]:
    """Run a command without a shell and return a JSON-friendly result.

    The supervisor must never die because an optional probe failed, so all
    OSError/timeout paths are converted to data.
    """
    argv_list = [str(part) for part in argv]
    started = time.monotonic()
    result: dict[str, Any] = {
        "argv": argv_list,
        "available": False,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "truncated": False,
        "error": None,
        "duration_s": 0.0,
    }
    try:
        completed = subprocess.run(
            argv_list,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout if timeout and timeout > 0 else None,
            cwd=str(cwd) if cwd else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        result["timed_out"] = True
        result["error"] = "timed out after {}s".format(timeout)
        result["stdout"] = decode_bytes(exc.stdout or b"")[:max_output]
        result["stderr"] = decode_bytes(exc.stderr or b"")[:max_output]
        result["duration_s"] = round(time.monotonic() - started, 3)
        return result
    except OSError as exc:
        result["error"] = "{}: {}".format(type(exc).__name__, exc)
        result["duration_s"] = round(time.monotonic() - started, 3)
        return result

    stdout = decode_bytes(completed.stdout or b"")
    stderr = decode_bytes(completed.stderr or b"")
    result["available"] = True
    result["returncode"] = completed.returncode
    if len(stdout) > max_output:
        stdout = stdout[:max_output] + "\n[crashmon: stdout truncated]\n"
        result["truncated"] = True
    if len(stderr) > max_output:
        stderr = stderr[:max_output] + "\n[crashmon: stderr truncated]\n"
        result["truncated"] = True
    result["stdout"] = stdout
    result["stderr"] = stderr
    result["duration_s"] = round(time.monotonic() - started, 3)
    return result


def iter_files(root: os.PathLike[str] | str, *, max_files: int = 2000) -> Iterable[Path]:
    """Walk a directory defensively (permission errors are skipped)."""
    base = Path(root)
    seen = 0
    for current, _dirnames, filenames in os.walk(str(base), onerror=lambda _error: None):
        for filename in filenames:
            seen += 1
            if seen > max_files:
                return
            yield Path(current) / filename


def file_mtime_epoch(path: os.PathLike[str] | str) -> Optional[float]:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def file_size(path: os.PathLike[str] | str) -> Optional[int]:
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def safe_stat_times(path: os.PathLike[str] | str) -> dict[str, Any]:
    try:
        stat = Path(path).stat()
    except OSError:
        return {"mtime_utc": None, "size_bytes": None}
    return {
        "mtime_utc": utc_iso(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)),
        "size_bytes": stat.st_size,
    }


def bool_text(value: Any) -> str:
    return "yes" if value else "no"


def format_seconds(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value < 1.0:
        return "{:.0f} ms".format(value * 1000)
    if value < 120.0:
        return "{:.1f} s".format(value)
    return "{:.1f} min".format(value / 60.0)


def python_info() -> dict[str, Any]:
    return {
        "version": sys.version.split()[0],
        "version_full": sys.version.replace("\n", " "),
        "executable": sys.executable,
        "implementation": sys.implementation.name,
    }
