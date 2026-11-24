"""Platform crash-artifact discovery and privacy-aware bundle materialization.

Collection is read-only with respect to the operating system: crashmon never
changes WER/LocalDumps registry values, ``core_pattern``, ulimits, or system
crash directories. Artifacts are copied into the bundle and hashed.

Binary dumps/cores cannot be redacted internally. Text reports (``.wer``,
``.ips``, ``.crash``) are decoded and redacted before being copied.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .redaction import Redactor
from .util import (
    CrashmonError,
    decode_maybe_utf16,
    ensure_dir,
    file_mtime_epoch,
    file_size,
    iter_files,
    read_text_limited,
    relpath_posix,
    run_command,
    sanitize_filename,
    sha256_and_size,
    unique_path,
    utc_iso,
    which,
)

ARTIFACT_EXTENSIONS = {
    ".dmp": "minidump",
    ".hdmp": "minidump",
    ".mdmp": "minidump",
    ".core": "core-dump",
    ".crash": "crash-report",
    ".ips": "crash-report",
    ".wer": "wer-report",
    ".diag": "diagnostic-report",
}
TEXT_EXTENSIONS = {".wer", ".ips", ".crash", ".diag", ".txt", ".log", ".json", ".md"}
TEXT_ARTIFACT_EXTENSIONS = {".wer", ".ips", ".crash", ".diag"}


def _write_text_file(path: Path, text: str) -> None:
    """Write redacted text with fixed newlines (Python 3.9-compatible)."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def normalize_process_name(value: str) -> str:
    base = Path(value).name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base or "blender"


def process_matches(text: str, process_name: str) -> bool:
    normalized = normalize_process_name(process_name)
    if normalized in ("", "*", "any"):
        return True
    return normalized in str(text).lower()


def within_window(mtime: Optional[float], cutoff_epoch: Optional[float]) -> bool:
    if cutoff_epoch is None:
        return True
    return mtime is not None and mtime >= cutoff_epoch


def _candidate(
    *,
    source: str,
    path: os.PathLike[str] | str,
    kind: str,
    note: str,
    text: bool,
    summary: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "path": str(path),
        "kind": kind,
        "note": note,
        "text": text,
        "summary": summary or {},
    }


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def local_appdata_dir() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if value:
        return Path(value)
    return Path.home() / "AppData" / "Local"


def windows_crashdumps_dir() -> Path:
    return local_appdata_dir() / "CrashDumps"


def read_localdumps_registry(errors: Optional[list[str]] = None) -> dict[str, Any]:
    """Read (never write) WER LocalDumps configuration."""
    errors = errors if errors is not None else []
    result: dict[str, Any] = {
        "supported": platform.system() == "Windows",
        "entries": [],
        "enabled_hint": False,
        "notes": [],
        "errors": errors,
    }
    if platform.system() != "Windows":
        result["notes"].append("Windows registry probe skipped on this platform.")
        return result
    try:
        import winreg  # noqa: PLC0415 - Windows only
    except ImportError:
        result["errors"].append("winreg import failed")
        return result

    base = r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps"
    roots = (("HKLM", winreg.HKEY_LOCAL_MACHINE), ("HKCU", winreg.HKEY_CURRENT_USER))
    for root_label, root in roots:
        for suffix in ("", "\\blender.exe", "\\blender"):
            key_path = base + suffix
            try:
                key = winreg.OpenKey(root, key_path, 0, winreg.KEY_READ)
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append("{} {}: {}".format(root_label, key_path, exc))
                continue
            try:
                values: dict[str, Any] = {}
                try:
                    _subkeys, value_count, _modified = winreg.QueryInfoKey(key)
                except OSError:
                    value_count = 0
                for index in range(value_count):
                    try:
                        name, value, _value_type = winreg.EnumValue(key, index)
                    except OSError:
                        continue
                    if isinstance(value, bytes):
                        value = value.decode("utf-16-le", errors="replace").rstrip("\x00")
                    values[name] = value
                entry = {"root": root_label, "key": key_path, "values": values}
                result["entries"].append(entry)
                if values.get("Enabled", 1) not in (0, "0") and (
                    "DumpFolder" in values or "DumpType" in values or values.get("Enabled") in (1, "1")
                ):
                    result["enabled_hint"] = True
            except OSError as exc:
                errors.append("{} {} values: {}".format(root_label, key_path, exc))
            finally:
                try:
                    winreg.CloseKey(key)
                except OSError:
                    pass
    if not result["entries"]:
        result["notes"].append(
            "No LocalDumps registry configuration found. Windows Error Reporting does not write "
            "local dumps by default; crashmon only reads this configuration."
        )
    else:
        result["notes"].append(
            "LocalDumps values are reported read-only; crashmon never creates or modifies them."
        )
    return result


def windows_wer_dirs() -> list[dict[str, Any]]:
    local = local_appdata_dir() / "Microsoft" / "Windows" / "WER"
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft" / "Windows" / "WER"
    return [
        {"label": "local-ReportArchive", "path": str(local / "ReportArchive")},
        {"label": "local-ReportQueue", "path": str(local / "ReportQueue")},
        {"label": "system-ReportArchive", "path": str(program_data / "ReportArchive")},
        {"label": "system-ReportQueue", "path": str(program_data / "ReportQueue")},
    ]


def discover_windows_crashdumps(
    crashdumps_dir: os.PathLike[str] | str,
    process_name: str,
    cutoff_epoch: Optional[float],
    errors: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    errors = errors if errors is not None else []
    directory = Path(crashdumps_dir)
    candidates: list[dict[str, Any]] = []
    if not directory.is_dir():
        return candidates
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        errors.append("CrashDumps listing {}: {}".format(directory, exc))
        return candidates
    for entry in entries:
        if not entry.is_file():
            continue
        if not process_matches(entry.name, process_name):
            continue
        mtime = file_mtime_epoch(entry)
        if not within_window(mtime, cutoff_epoch):
            continue
        candidates.append(
            _candidate(
                source="windows-crashdumps",
                path=entry,
                kind="minidump",
                note="File from %LOCALAPPDATA%\\CrashDumps matching the supervised process.",
                text=False,
            )
        )
    return candidates


def discover_wer_reports(
    directories: Iterable[dict[str, Any]],
    process_name: str,
    cutoff_epoch: Optional[float],
    errors: Optional[list[str]] = None,
    max_reports: int = 300,
) -> list[dict[str, Any]]:
    """Find WER report folders whose Report.wer mentions the process."""
    errors = errors if errors is not None else []
    candidates: list[dict[str, Any]] = []
    for descriptor in directories:
        base = Path(descriptor["path"])
        if not base.is_dir():
            continue
        try:
            report_dirs = [item for item in base.iterdir() if item.is_dir()]
        except OSError as exc:
            errors.append("WER listing {}: {}".format(base, exc))
            continue
        report_dirs.sort(key=lambda item: file_mtime_epoch(item) or 0.0, reverse=True)
        for report_dir in report_dirs[:max_reports]:
            wer_file = report_dir / "Report.wer"
            if not wer_file.is_file():
                continue
            mtime = file_mtime_epoch(wer_file)
            if not within_window(mtime, cutoff_epoch):
                continue
            text = read_text_limited(wer_file, limit=131072)
            matched = process_matches(text, process_name) or process_matches(report_dir.name, process_name)
            if not matched:
                continue
            summary = {"wer_report_dir": report_dir.name}
            candidates.append(
                _candidate(
                    source="windows-wer",
                    path=wer_file,
                    kind="wer-report",
                    note="WER Report.wer mentioning the process; text is redacted when copied.",
                    text=True,
                    summary=summary,
                )
            )
            try:
                for child in report_dir.iterdir():
                    if child.is_file() and child.suffix.lower() in (".dmp", ".hdmp", ".mdmp"):
                        candidates.append(
                            _candidate(
                                source="windows-wer",
                                path=child,
                                kind="minidump",
                                note="Dump file inside a WER report folder for the process.",
                                text=False,
                                summary=summary,
                            )
                        )
            except OSError as exc:
                errors.append("WER report listing {}: {}".format(report_dir, exc))
    return candidates


def windows_platform_info(
    process_name: str = "blender",
    cutoff_epoch: Optional[float] = None,
    crashdumps_dir: Optional[os.PathLike[str] | str] = None,
) -> dict[str, Any]:
    errors: list[str] = []
    directory = Path(crashdumps_dir) if crashdumps_dir else windows_crashdumps_dir()
    recent_matches = 0
    try:
        if directory.is_dir():
            for item in directory.iterdir():
                if item.is_file() and process_matches(item.name, process_name):
                    if within_window(file_mtime_epoch(item), cutoff_epoch):
                        recent_matches += 1
    except OSError as exc:
        errors.append("CrashDumps scan: {}".format(exc))
    wer_descriptors = windows_wer_dirs()
    wer_info = []
    for descriptor in wer_descriptors:
        path = Path(descriptor["path"])
        wer_info.append(
            {
                "label": descriptor["label"],
                "path": str(path),
                "exists": path.is_dir(),
                "readable": os.access(str(path), os.R_OK) if path.exists() else False,
            }
        )
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    werfault = system_root / "System32" / "WerFault.exe"
    result = {
        "supported": platform.system() == "Windows",
        "crashdumps": {
            "path": str(directory),
            "exists": directory.is_dir(),
            "readable": os.access(str(directory), os.R_OK) if directory.exists() else False,
            "recent_matching_files": recent_matches,
        },
        "localdumps_registry": read_localdumps_registry(errors),
        "wer_dirs": wer_info,
        "werfault": {"path": str(werfault), "exists": werfault.is_file()},
        "available": directory.is_dir() or any(item["exists"] for item in wer_info),
        "notes": [
            "Windows local dumps require WER LocalDumps configuration or a WER report; "
            "a missing dump is not evidence that no crash occurred.",
            "Crashmon reads WER configuration and directories but never changes them.",
        ],
        "errors": errors,
    }
    return result


def discover_windows_artifacts(
    process_name: str,
    cutoff_epoch: Optional[float],
    errors: Optional[list[str]] = None,
    crashdumps_dir: Optional[os.PathLike[str] | str] = None,
    max_reports: int = 300,
) -> list[dict[str, Any]]:
    errors = errors if errors is not None else []
    directory = Path(crashdumps_dir) if crashdumps_dir else windows_crashdumps_dir()
    candidates = discover_windows_crashdumps(directory, process_name, cutoff_epoch, errors)
    candidates.extend(discover_wer_reports(windows_wer_dirs(), process_name, cutoff_epoch, errors, max_reports))
    return candidates


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


def _read_core_pattern() -> tuple[Optional[str], Optional[str]]:
    try:
        return Path("/proc/sys/kernel/core_pattern").read_text(encoding="utf-8", errors="replace").strip(), None
    except OSError as exc:
        return None, str(exc)


def _core_limit() -> dict[str, Any]:
    try:
        import resource  # noqa: PLC0415 - POSIX only

        soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        return {
            "soft": None if soft == resource.RLIM_INFINITY else int(soft),
            "hard": None if hard == resource.RLIM_INFINITY else int(hard),
            "soft_text": "unlimited" if soft == resource.RLIM_INFINITY else str(soft),
            "hard_text": "unlimited" if hard == resource.RLIM_INFINITY else str(hard),
            "available": True,
        }
    except Exception as exc:  # pragma: no cover - non-POSIX
        return {"available": False, "error": str(exc)}


def _parse_coredumpctl_matches(stdout: str, process_name: str, limit: int = 10) -> tuple[int, list[str]]:
    lines = [line.rstrip() for line in stdout.splitlines() if line.strip()]
    matched = [line for line in lines if process_matches(line, process_name)]
    return len(lines), matched[:limit]


def linux_platform_info(
    process_name: str = "blender",
    cutoff_epoch: Optional[float] = None,
    *,
    which_func: Callable[[str], Optional[str]] = which,
    runner: Callable[..., dict[str, Any]] = run_command,
    core_pattern_reader: Callable[[], tuple[Optional[str], Optional[str]]] = _read_core_pattern,
    core_limit_getter: Callable[[], dict[str, Any]] = _core_limit,
    systemd_coredump_dir: str = "/var/lib/systemd/coredump",
    errors: Optional[list[str]] = None,
) -> dict[str, Any]:
    errors = errors if errors is not None else []
    coredumpctl = which_func("coredumpctl")
    coredump_info: dict[str, Any] = {
        "available": bool(coredumpctl),
        "path": coredumpctl,
        "list_ok": False,
        "entry_count": None,
        "matching_lines": [],
        "error": None,
    }
    if coredumpctl:
        command = runner(
            [coredumpctl, "--no-pager", "--no-legend", "list"],
            timeout=10,
        )
        coredump_info["list_ok"] = command.get("returncode") == 0
        if command.get("returncode") == 0:
            count, matches = _parse_coredumpctl_matches(command.get("stdout", ""), process_name)
            coredump_info["entry_count"] = count
            coredump_info["matching_lines"] = matches
        else:
            coredump_info["error"] = command.get("error") or command.get("stderr", "").strip() or "coredumpctl list failed"
            errors.append("coredumpctl list: {}".format(coredump_info["error"]))

    core_pattern, pattern_error = core_pattern_reader()
    if pattern_error:
        errors.append("core_pattern: {}".format(pattern_error))
    core_pattern_info: dict[str, Any] = {
        "path": "/proc/sys/kernel/core_pattern",
        "value": core_pattern,
        "readable": core_pattern is not None,
        "piped": bool(core_pattern and core_pattern.startswith("|")),
        "error": pattern_error,
    }

    limits = core_limit_getter()
    systemd_dir = Path(systemd_coredump_dir)
    result = {
        "supported": platform.system() == "Linux",
        "coredumpctl": coredump_info,
        "core_pattern": core_pattern_info,
        "ulimit_core": limits,
        "systemd_coredump_dir": {
            "path": str(systemd_dir),
            "exists": systemd_dir.is_dir(),
            "readable": os.access(str(systemd_dir), os.R_OK) if systemd_dir.exists() else False,
        },
        "available": bool(coredumpctl) or core_pattern is not None,
        "notes": [
            "Crashmon never modifies core_pattern or ulimit; core availability depends on distro policy, "
            "container settings, and the launching shell.",
            "Piped core patterns (apport, systemd-coredump) may not leave a file crashmon can copy. "
            "System cores are only extracted with the explicit --include-system-cores flag.",
            "Core extraction via coredumpctl may require privileges or polkit authorization.",
        ],
        "errors": errors,
    }
    return result


def copy_latest_linux_core(
    process_name: str,
    destination: os.PathLike[str] | str,
    *,
    which_func: Callable[[str], Optional[str]] = which,
    runner: Callable[..., dict[str, Any]] = run_command,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Explicit opt-in: ask coredumpctl for the latest matching core."""
    destination_path = Path(destination)
    ensure_dir(destination_path.parent)
    coredumpctl = which_func("coredumpctl")
    if not coredumpctl:
        return {"ok": False, "error": "coredumpctl not available", "path": str(destination_path)}
    if destination_path.exists():
        try:
            destination_path.unlink()
        except OSError as exc:
            return {"ok": False, "error": "cannot replace destination: {}".format(exc), "path": str(destination_path)}
    command = runner(
        [
            coredumpctl,
            "--no-pager",
            "dump",
            "--output",
            str(destination_path),
            normalize_process_name(process_name),
        ],
        timeout=timeout,
    )
    ok = command.get("returncode") == 0 and destination_path.is_file()
    result = {
        "ok": ok,
        "path": str(destination_path),
        "size_bytes": file_size(destination_path) if ok else None,
        "error": None if ok else (command.get("error") or command.get("stderr", "").strip() or "coredumpctl dump failed"),
    }
    if not ok and destination_path.exists():
        try:
            destination_path.unlink()
        except OSError:
            pass
    return result


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


def macos_diagnostic_dirs() -> list[dict[str, Any]]:
    return [
        {"label": "user", "path": str(Path.home() / "Library" / "Logs" / "DiagnosticReports")},
        {"label": "system", "path": "/Library/Logs/DiagnosticReports"},
    ]


def _macos_report_summary(path: Path) -> dict[str, Any]:
    """Read only a small header from .ips/.crash files."""
    summary: dict[str, Any] = {}
    try:
        with open(path, "rb") as handle:
            head = handle.read(8192)
    except OSError:
        return summary
    text = decode_maybe_utf16(head)
    if path.suffix.lower() == ".ips":
        first_line = text.splitlines()[0] if text.splitlines() else ""
        try:
            header = json.loads(first_line)
        except (json.JSONDecodeError, IndexError):
            header = None
        if isinstance(header, dict):
            for key in ("app_name", "name", "procName", "timestamp", "bug_type", "os_version", "incident_id"):
                value = header.get(key)
                if isinstance(value, (str, int, float)):
                    summary[key] = value
    process_line = re.search(r"^Process:\s*(.+)$", text, re.MULTILINE)
    if process_line:
        summary["process_header"] = process_line.group(1).strip()[:200]
    return summary


def discover_macos_reports(
    directories: Iterable[dict[str, Any]],
    process_name: str,
    cutoff_epoch: Optional[float],
    errors: Optional[list[str]] = None,
    max_files: int = 500,
) -> list[dict[str, Any]]:
    errors = errors if errors is not None else []
    candidates: list[dict[str, Any]] = []
    for descriptor in directories:
        base = Path(descriptor["path"])
        if not base.is_dir():
            continue
        try:
            files = [item for item in base.iterdir() if item.is_file()]
        except OSError as exc:
            errors.append("DiagnosticReports listing {}: {}".format(base, exc))
            continue
        files.sort(key=lambda item: file_mtime_epoch(item) or 0.0, reverse=True)
        for item in files[:max_files]:
            if item.suffix.lower() not in (".crash", ".ips", ".diag"):
                continue
            mtime = file_mtime_epoch(item)
            if not within_window(mtime, cutoff_epoch):
                continue
            summary = _macos_report_summary(item)
            matched = process_matches(item.name, process_name)
            if not matched:
                matched = any(
                    process_matches(str(value), process_name) for value in summary.values() if isinstance(value, str)
                )
            if not matched:
                continue
            candidates.append(
                _candidate(
                    source="macos-diagnostic-reports",
                    path=item,
                    kind="crash-report",
                    note="macOS DiagnosticReports entry for the process; text is redacted when copied.",
                    text=True,
                    summary=summary,
                )
            )
    return candidates


def macos_platform_info(
    process_name: str = "blender",
    cutoff_epoch: Optional[float] = None,
    directories: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    errors: list[str] = []
    descriptors = directories or macos_diagnostic_dirs()
    dirs_info: list[dict[str, Any]] = []
    recent_total = 0
    for descriptor in descriptors:
        path = Path(descriptor["path"])
        count = 0
        if path.is_dir():
            try:
                for item in path.iterdir():
                    if item.is_file() and item.suffix.lower() in (".crash", ".ips", ".diag"):
                        if within_window(file_mtime_epoch(item), cutoff_epoch):
                            count += 1
            except OSError as exc:
                errors.append("DiagnosticReports scan {}: {}".format(path, exc))
        recent_total += count
        dirs_info.append(
            {
                "label": descriptor["label"],
                "path": str(path),
                "exists": path.is_dir(),
                "readable": os.access(str(path), os.R_OK) if path.exists() else False,
                "recent_report_files": count,
            }
        )
    result = {
        "supported": platform.system() == "Darwin",
        "diagnostic_report_dirs": dirs_info,
        "recent_report_files": recent_total,
        "available": any(item["exists"] for item in dirs_info),
        "notes": [
            "macOS writes DiagnosticReports asynchronously; a report may appear only after a delay. "
            "Run collect again later if a crash report is missing.",
            "The system-wide /Library/Logs/DiagnosticReports directory may require elevated read access; "
            "permission errors are recorded, not bypassed.",
        ],
        "errors": errors,
    }
    return result


# ---------------------------------------------------------------------------
# Generic crash directories and user-supplied logs
# ---------------------------------------------------------------------------


def discover_generic_crash_dirs(
    directories: Iterable[os.PathLike[str] | str],
    process_name: str,
    cutoff_epoch: Optional[float],
    errors: Optional[list[str]] = None,
    max_files: int = 2000,
) -> list[dict[str, Any]]:
    """Scan explicitly supplied crash directories for candidate files.

    A file is kept only if the process name appears somewhere in its path (or
    the process name is ``*``), which avoids copying unrelated crashes.
    """
    errors = errors if errors is not None else []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_directory in directories:
        directory = Path(raw_directory).expanduser()
        if not directory.exists():
            errors.append("Crash directory does not exist: {}".format(directory))
            continue
        files = [directory] if directory.is_file() else list(iter_files(directory, max_files=max_files))
        for item in files:
            suffix = item.suffix.lower()
            is_core = item.name.lower().startswith("core")
            if suffix not in ARTIFACT_EXTENSIONS and not is_core:
                continue
            if not process_matches(str(item), process_name):
                continue
            if not within_window(file_mtime_epoch(item), cutoff_epoch):
                continue
            try:
                key = str(item.resolve())
            except OSError:
                key = str(item)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                _candidate(
                    source="user-crash-dir",
                    path=item,
                    kind=ARTIFACT_EXTENSIONS.get(suffix, "core-dump"),
                    note="User-supplied crash directory.",
                    text=suffix in TEXT_ARTIFACT_EXTENSIONS,
                )
            )
    return candidates


def platform_platform_info(
    system: Optional[str] = None,
    *,
    process_name: str = "blender",
    cutoff_epoch: Optional[float] = None,
) -> dict[str, Any]:
    """Return capability metadata for the current platform only."""
    system = system or platform.system()
    if system == "Windows":
        return {"system": system, "windows": windows_platform_info(process_name, cutoff_epoch)}
    if system == "Linux":
        return {"system": system, "linux": linux_platform_info(process_name, cutoff_epoch)}
    if system == "Darwin":
        return {"system": system, "macos": macos_platform_info(process_name, cutoff_epoch)}
    return {
        "system": system,
        "supported": False,
        "notes": ["No platform-specific crash-artifact collector is implemented for {}.".format(system)],
    }


def discover_platform_artifacts(
    system: Optional[str] = None,
    *,
    process_name: str = "blender",
    cutoff_epoch: Optional[float] = None,
    crashdumps_dir: Optional[os.PathLike[str] | str] = None,
    errors: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    system = system or platform.system()
    errors = errors if errors is not None else []
    if system == "Windows":
        return discover_windows_artifacts(process_name, cutoff_epoch, errors, crashdumps_dir)
    if system == "Linux":
        # Linux system cores are opt-in via --include-system-cores; discovery
        # here returns no system core paths (they live under /var/lib/systemd).
        return []
    if system == "Darwin":
        return discover_macos_reports(macos_diagnostic_dirs(), process_name, cutoff_epoch, errors)
    return []


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def _artifact_entry(
    *,
    source: str,
    kind: str,
    original_path: str,
    relative_path: Optional[str],
    copied: bool,
    size_bytes: Optional[int],
    sha256: Optional[str],
    mtime: Optional[float],
    note: str,
    text_redacted: bool = False,
    original_sha256: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "kind": kind,
        "original_path": original_path,
        "relative_path": relative_path,
        "copied": copied,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "original_sha256": original_sha256,
        "mtime_utc": utc_iso(datetime.fromtimestamp(mtime, tz=timezone.utc)) if mtime else None,
        "text_redacted": text_redacted,
        "note": note,
    }


def materialize_candidates(
    candidates: list[dict[str, Any]],
    bundle_dir: os.PathLike[str] | str,
    redactor: Redactor,
    *,
    max_artifact_bytes: int = 64 * 1024 * 1024,
    max_total_artifact_bytes: int = 256 * 1024 * 1024,
    max_artifacts: int = 20,
    subdir: str = "crash",
) -> list[dict[str, Any]]:
    """Copy candidates into ``<bundle>/crash`` and return manifest entries."""
    bundle = Path(bundle_dir)
    crash_dir = ensure_dir(bundle / subdir)
    entries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    copied_count = 0
    copied_bytes = 0

    # Stable order makes bundles reproducible dumps.
    ordered = sorted(candidates, key=lambda item: (str(item.get("source", "")), str(item.get("path", ""))))
    for candidate in ordered:
        source = str(candidate.get("source") or "unknown")
        kind = str(candidate.get("kind") or "artifact")
        note = str(candidate.get("note") or "")
        source_path = Path(str(candidate.get("path")))
        try:
            key = str(source_path.resolve())
        except OSError:
            key = str(source_path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        mtime = file_mtime_epoch(source_path)
        size = file_size(source_path)
        original_display = str(source_path)
        if size is None:
            entries.append(
                _artifact_entry(
                    source=source,
                    kind=kind,
                    original_path=original_display,
                    relative_path=None,
                    copied=False,
                    size_bytes=None,
                    sha256=None,
                    mtime=mtime,
                    note="Unreadable artifact: {}".format(source_path),
                )
            )
            continue
        if size > max_artifact_bytes:
            entries.append(
                _artifact_entry(
                    source=source,
                    kind=kind,
                    original_path=original_display,
                    relative_path=None,
                    copied=False,
                    size_bytes=size,
                    sha256=None,
                    mtime=mtime,
                    note="Skipped: artifact exceeds --max-artifact-bytes ({} > {}).".format(
                        size, max_artifact_bytes
                    ),
                )
            )
            continue
        if copied_count >= max_artifacts:
            entries.append(
                _artifact_entry(
                    source=source,
                    kind=kind,
                    original_path=original_display,
                    relative_path=None,
                    copied=False,
                    size_bytes=size,
                    sha256=None,
                    mtime=mtime,
                    note="Skipped: --max-artifacts ({}) reached.".format(max_artifacts),
                )
            )
            continue
        if copied_bytes + size > max_total_artifact_bytes:
            entries.append(
                _artifact_entry(
                    source=source,
                    kind=kind,
                    original_path=original_display,
                    relative_path=None,
                    copied=False,
                    size_bytes=size,
                    sha256=None,
                    mtime=mtime,
                    note="Skipped: --max-total-artifact-bytes would be exceeded.",
                )
            )
            continue

        prefix = sanitize_filename(source)
        destination = unique_path(crash_dir, "{}-{}".format(prefix, sanitize_filename(source_path.name)))
        is_text = bool(candidate.get("text")) or source_path.suffix.lower() in TEXT_ARTIFACT_EXTENSIONS
        try:
            if is_text:
                raw = source_path.read_bytes()
                decoded = decode_maybe_utf16(raw)
                redacted_text = redactor.redact(decoded)
                _write_text_file(destination, redacted_text)
                digest, stored_size = sha256_and_size(destination)
                original_digest = hashlib.sha256(raw).hexdigest()
                entries.append(
                    _artifact_entry(
                        source=source,
                        kind=kind,
                        original_path=original_display,
                        relative_path=relpath_posix(destination, bundle),
                        copied=True,
                        size_bytes=stored_size,
                        sha256=digest,
                        mtime=mtime,
                        note=(note + " Text content redacted in the bundle copy.").strip(),
                        text_redacted=True,
                        original_sha256=original_digest,
                    )
                )
                copied_bytes += stored_size
            else:
                digest = hashlib.sha256()
                written = 0
                with open(source_path, "rb") as source_handle, open(destination, "wb") as dest_handle:
                    while True:
                        chunk = source_handle.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_artifact_bytes:
                            raise CrashmonError("artifact grew beyond the configured size limit")
                        digest.update(chunk)
                        dest_handle.write(chunk)
                entries.append(
                    _artifact_entry(
                        source=source,
                        kind=kind,
                        original_path=original_display,
                        relative_path=relpath_posix(destination, bundle),
                        copied=True,
                        size_bytes=written,
                        sha256=digest.hexdigest(),
                        mtime=mtime,
                        note=(note + " Binary artifact copied verbatim; contents are not redactable.").strip(),
                    )
                )
                copied_bytes += written
            copied_count += 1
        except (OSError, CrashmonError) as exc:
            try:
                if destination.exists():
                    destination.unlink()
            except OSError:
                pass
            entries.append(
                _artifact_entry(
                    source=source,
                    kind=kind,
                    original_path=original_display,
                    relative_path=None,
                    copied=False,
                    size_bytes=size,
                    sha256=None,
                    mtime=mtime,
                    note="Copy failed: {}".format(exc),
                )
            )
    return entries


def register_bundle_file(
    path: os.PathLike[str] | str,
    bundle_dir: os.PathLike[str] | str,
    redactor: Redactor,
    *,
    source: str,
    kind: str,
    note: str,
    original_path: Optional[str] = None,
    text: bool = False,
) -> dict[str, Any]:
    """Register a file that is already inside the bundle (e.g. a core dump)."""
    bundle = Path(bundle_dir)
    item = Path(path)
    mtime = file_mtime_epoch(item)
    size = file_size(item)
    if size is None:
        return _artifact_entry(
            source=source,
            kind=kind,
            original_path=original_path or str(item),
            relative_path=None,
            copied=False,
            size_bytes=None,
            sha256=None,
            mtime=mtime,
            note="Unreadable bundle artifact: {}".format(item),
        )
    text_redacted = False
    original_digest = None
    if text:
        raw = item.read_bytes()
        original_digest = hashlib.sha256(raw).hexdigest()
        _write_text_file(item, redactor.redact(decode_maybe_utf16(raw)))
        text_redacted = True
    digest, stored_size = sha256_and_size(item)
    return _artifact_entry(
        source=source,
        kind=kind,
        original_path=original_path or "<collected directly into bundle>",
        relative_path=relpath_posix(item, bundle),
        copied=True,
        size_bytes=stored_size,
        sha256=digest,
        mtime=mtime,
        note=note,
        text_redacted=text_redacted,
        original_sha256=original_digest,
    )


def import_log_file(
    source: os.PathLike[str] | str,
    bundle_dir: os.PathLike[str] | str,
    redactor: Redactor,
    *,
    stream: str = "imported",
    tail_lines: int = 80,
    max_bytes: int = 64 * 1024 * 1024,
) -> dict[str, Any]:
    """Copy an existing log into the bundle, redacting text and noting truncation."""
    bundle = Path(bundle_dir)
    source_path = Path(source)
    logs_dir = ensure_dir(bundle / "logs")
    if not source_path.is_file():
        raise CrashmonError("Log file not found: {}".format(source_path))
    size = file_size(source_path) or 0
    note = "Imported by collect."
    truncated = size > max_bytes
    if truncated:
        with open(source_path, "rb") as handle:
            handle.seek(max(0, size - max_bytes))
            raw = handle.read(max_bytes)
        note += " Imported the last {} of {} bytes.".format(len(raw), size)
    else:
        raw = source_path.read_bytes()
    text = decode_maybe_utf16(raw)
    redacted_text = redactor.redact(text)
    destination = unique_path(logs_dir, "imported-{}".format(sanitize_filename(source_path.name, "log.txt")))
    _write_text_file(destination, redacted_text)
    digest, stored_size = sha256_and_size(destination)
    tail = [line.rstrip("\r\n") for line in redacted_text.splitlines()[-tail_lines:]]
    return {
        "stream": stream,
        "source_path": str(source_path),
        "relative_path": relpath_posix(destination, bundle),
        "size_bytes": stored_size,
        "sha256": digest,
        "retained": True,
        "tail_only": False,
        "truncated": truncated,
        "tail": tail,
        "files": [
            {
                "relative_path": relpath_posix(destination, bundle),
                "size_bytes": stored_size,
                "sha256": digest,
            }
        ],
        "rotation": {"max_bytes": max_bytes, "backups": 0, "rotated": False},
        "note": note,
    }
