"""Bundle creation, manifest assembly, and schema validation.

A bundle is a directory containing ``manifest.json`` (the auditable record),
``logs/`` (retained or tail-only logs), and ``crash/`` (copied artifacts).
Manifests are plain JSON and intentionally contain no absolute home paths after
redaction.
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import SCHEMA_VERSION, __version__
from .redaction import Redactor
from .util import (
    CrashmonError,
    atomic_write_json,
    ensure_dir,
    local_iso,
    read_json,
    sanitize_filename,
    timezone_name,
    utc_iso,
    utc_now,
)

ENV_ALLOWLIST = (
    "BLENDER_USER_CONFIG",
    "BLENDER_USER_SCRIPTS",
    "BLENDER_USER_EXTENSIONS",
    "BLENDER_SYSTEM_SCRIPTS",
    "BLENDER_SYSTEM_PYTHON",
    "BLENDER_VERBOSE",
    "CUDA_VISIBLE_DEVICES",
    "HIP_VISIBLE_DEVICES",
    "ROCR_VISIBLE_DEVICES",
    "METAL_DEVICE_WRAPPER_TYPE",
    "VK_ICD_FILENAMES",
    "VK_LOADER_DEBUG",
    "OCIO",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_SESSION_TYPE",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "PYTHONPATH",
    "PYTHONHOME",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "PROCESSOR_ARCHITECTURE",
    "OS",
)

LIMITATIONS = (
    "The supervisor observes the direct Blender child process only. Crashes in detached child processes, "
    "GPU hang/TDR events, or machine-wide failures may not appear in the bundle.",
    "A missing crash artifact is not proof that no crash occurred. OS dump policy, permissions, and "
    "reporting latency can suppress or delay artifacts.",
    "Minidumps and cores are not symbolized. Reconstructing stacks needs matching debug symbols (PDB/DWARF) "
    "and the exact build ID/commit of the Blender binary.",
    "Windows WER/LocalDumps does not write local dumps by default; Linux cores depend on core_pattern, "
    "ulimit, systemd-coredump/apport, and container permissions; macOS crash reports are written asynchronously.",
    "Binary dumps/cores are copied verbatim and may contain process memory, paths, and user data. "
    "Redaction only covers text logs and text crash reports.",
    "This prototype never performs network I/O and has no upload backend. A production system needs "
    "Crashpad/Sentry/Breakpad, a symbol server, retention controls, and an authenticated intake service.",
    "Exit codes are interpreted with best-effort platform mappings; shell wrappers may translate signals "
    "into 128+N exit codes that cannot be distinguished from ordinary exit statuses.",
)


def default_upload_section() -> dict[str, Any]:
    return {
        "requested": False,
        "consent": False,
        "mode": "disabled",
        "status": "not-requested",
        "network_performed": False,
        "target": None,
        "file_count": 0,
        "total_bytes": 0,
        "plan_file": None,
        "boundary": (
            "Uploads are disabled by default. This prototype never opens a network socket; "
            "even with --upload-url and --consent it writes only a dry-run upload-plan.json."
        ),
    }


def collect_environment() -> dict[str, Any]:
    """Capture only allowlisted environment values; never the full environment."""
    values: dict[str, str] = {}
    for key in ENV_ALLOWLIST:
        if key not in os.environ:
            continue
        value = os.environ[key]
        if len(value) > 2048:
            value = value[:2048] + "...[truncated by crashmon]"
        values[key] = value
    return {
        "scope": "allowlist",
        "considered_keys": list(ENV_ALLOWLIST),
        "values": values,
        "present_count": len(values),
        "excluded_key_count": max(0, len(os.environ) - len(values)),
        "note": "Only the allowlisted keys above are stored; the full environment is never captured.",
    }


def make_manifest(
    *,
    kind: str,
    bundle_dir: os.PathLike[str] | str,
    redactor: Redactor,
    raw_logs_included: bool = True,
    environment: Optional[dict[str, Any]] = None,
    system: Optional[dict[str, Any]] = None,
    gpu: Optional[dict[str, Any]] = None,
    blender: Optional[dict[str, Any]] = None,
    platform_info: Optional[dict[str, Any]] = None,
    run: Optional[dict[str, Any]] = None,
    logs: Optional[list[dict[str, Any]]] = None,
    crash_artifacts: Optional[list[dict[str, Any]]] = None,
    upload: Optional[dict[str, Any]] = None,
    notes: Optional[list[str]] = None,
) -> dict[str, Any]:
    bundle = Path(bundle_dir)
    created = utc_now()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": {
            "name": "crashmon",
            "version": __version__,
            "prototype": True,
            "python": platform.python_version(),
            "executable": sys.executable,
        },
        "bundle": {
            "id": bundle.name,
            "kind": kind,
            "created_utc": utc_iso(created),
            "created_local": local_iso(created),
            "timezone": timezone_name(),
            "manifest_path": "manifest.json",
        },
        "privacy": {
            "level": redactor.level,
            "raw_logs_included": bool(raw_logs_included),
            "environment_scope": "allowlist",
            "network": "disabled",
            "redaction": {
                "level": redactor.level,
                "applied": redactor.applied,
                "replacements": 0,
                "placeholders": redactor.placeholders,
            },
            "notes": [
                "Text logs and text crash reports are redacted at capture time.",
                "Binary dumps/cores are copied verbatim; redaction cannot reach inside process memory.",
                "The full environment is never captured; only allowlisted keys are stored.",
                "Nothing is uploaded by this prototype.",
            ],
        },
        "run": run,
        "system": system or {},
        "gpu": gpu or {},
        "blender": blender or {},
        "environment": environment or collect_environment(),
        "logs": logs or [],
        "crash_artifacts": crash_artifacts or [],
        "platform": platform_info or {},
        "upload": upload or default_upload_section(),
        "limitations": list(LIMITATIONS),
        "notes": notes or [],
    }
    return manifest


def create_bundle(
    output_dir: os.PathLike[str] | str,
    kind: str,
    name: Optional[str] = None,
) -> Path:
    base = ensure_dir(Path(output_dir).expanduser())
    if name:
        bundle_name = sanitize_filename(name, "bundle")
        if not bundle_name.lower().startswith("crashmon"):
            bundle_name = "crashmon-{}-{}".format(kind, bundle_name)
    else:
        bundle_name = "crashmon-{}-{}-{}".format(
            kind,
            utc_now().strftime("%Y%m%dT%H%M%SZ"),
            secrets.token_hex(3),
        )
    candidate = base / bundle_name
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = base / "{}-{}".format(bundle_name, counter)
    ensure_dir(candidate)
    ensure_dir(candidate / "logs")
    ensure_dir(candidate / "crash")
    return candidate


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def finalize_manifest(
    bundle_dir: os.PathLike[str] | str,
    manifest: dict[str, Any],
    redactor: Redactor,
) -> dict[str, Any]:
    """Redact, validate, and atomically write ``manifest.json``."""
    bundle = Path(bundle_dir)
    payload = _to_jsonable(manifest)
    payload = redactor.redact_obj(payload)
    try:
        privacy = payload["privacy"]  # type: ignore[index]
        privacy["redaction"]["level"] = redactor.level
        privacy["redaction"]["applied"] = redactor.applied
        privacy["redaction"]["replacements"] = redactor.replacements
        privacy["redaction"]["placeholders"] = redactor.placeholders
    except (KeyError, TypeError) as exc:  # pragma: no cover - programmer error
        raise CrashmonError("manifest missing privacy section: {}".format(exc))
    problems = validate_manifest(payload)
    if problems:
        raise CrashmonError("manifest failed schema validation: {}".format("; ".join(problems)))
    atomic_write_json(bundle / "manifest.json", payload)
    return payload


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _check_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        return False
    parts = value.replace("\\", "/").split("/")
    return ".." not in parts


def validate_manifest(manifest: Any) -> list[str]:
    """Return a list of human-readable schema problems (empty means valid)."""
    problems: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest is not a JSON object"]
    required_types: dict[str, type] = {
        "schema_version": str,
        "tool": dict,
        "bundle": dict,
        "privacy": dict,
        "system": dict,
        "gpu": dict,
        "blender": dict,
        "environment": dict,
        "logs": list,
        "crash_artifacts": list,
        "platform": dict,
        "upload": dict,
        "limitations": list,
    }
    for key, expected in required_types.items():
        if key not in manifest:
            problems.append("missing top-level key: {}".format(key))
        elif not isinstance(manifest[key], expected):
            problems.append("{} has the wrong type".format(key))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append("schema_version must be {!r}".format(SCHEMA_VERSION))
    if "run" not in manifest:
        problems.append("missing top-level key: run")
    elif manifest["run"] is not None and not isinstance(manifest["run"], dict):
        problems.append("run must be an object or null")
    bundle = manifest.get("bundle")
    if isinstance(bundle, dict):
        for key in ("id", "kind", "created_utc"):
            if not isinstance(bundle.get(key), str) or not bundle.get(key):
                problems.append("bundle.{} is missing or invalid".format(key))
    privacy = manifest.get("privacy")
    if isinstance(privacy, dict):
        if privacy.get("level") not in Redactor.LEVELS:
            problems.append("privacy.level is invalid")
        if not isinstance(privacy.get("redaction"), dict):
            problems.append("privacy.redaction is missing")
        if privacy.get("raw_logs_included") is not None and not isinstance(
            privacy.get("raw_logs_included"), bool
        ):
            problems.append("privacy.raw_logs_included must be a boolean")
    environment = manifest.get("environment")
    if isinstance(environment, dict):
        if environment.get("scope") != "allowlist":
            problems.append("environment.scope must be 'allowlist'")
        if not isinstance(environment.get("values"), dict):
            problems.append("environment.values must be an object")
    logs = manifest.get("logs")
    if isinstance(logs, list):
        for index, entry in enumerate(logs):
            prefix = "logs[{}]".format(index)
            if not isinstance(entry, dict):
                problems.append(prefix + " is not an object")
                continue
            if not isinstance(entry.get("stream"), str):
                problems.append(prefix + ".stream is missing")
            if entry.get("retained"):
                relative = entry.get("relative_path")
                if not _check_relative_path(relative):
                    problems.append(prefix + ".relative_path is invalid")
                if not _HEX64.match(str(entry.get("sha256") or "")):
                    problems.append(prefix + ".sha256 is invalid")
            if not isinstance(entry.get("tail"), list):
                problems.append(prefix + ".tail must be a list")
    artifacts = manifest.get("crash_artifacts")
    if isinstance(artifacts, list):
        for index, entry in enumerate(artifacts):
            prefix = "crash_artifacts[{}]".format(index)
            if not isinstance(entry, dict):
                problems.append(prefix + " is not an object")
                continue
            if not isinstance(entry.get("source"), str) or not isinstance(entry.get("kind"), str):
                problems.append(prefix + " is missing source/kind")
            if entry.get("copied"):
                if not _check_relative_path(entry.get("relative_path")):
                    problems.append(prefix + ".relative_path is invalid")
                if not _HEX64.match(str(entry.get("sha256") or "")):
                    problems.append(prefix + ".sha256 is invalid")
            if entry.get("size_bytes") is not None and not isinstance(entry.get("size_bytes"), int):
                problems.append(prefix + ".size_bytes must be an integer or null")
    upload = manifest.get("upload")
    if isinstance(upload, dict):
        if upload.get("network_performed") is not False:
            problems.append("upload.network_performed must be false in this prototype")
        if upload.get("requested") and not upload.get("consent"):
            problems.append("upload.requested requires upload.consent")
    return problems


def load_manifest(bundle_dir: os.PathLike[str] | str, *, strict: bool = False) -> tuple[dict[str, Any], list[str]]:
    bundle = Path(bundle_dir).expanduser()
    path = bundle / "manifest.json" if bundle.is_dir() else bundle
    if not path.is_file():
        raise CrashmonError("manifest.json not found in bundle: {}".format(bundle))
    try:
        manifest = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise CrashmonError("cannot read manifest {}: {}".format(path, exc))
    problems = validate_manifest(manifest)
    if strict and problems:
        raise CrashmonError("manifest failed validation: {}".format("; ".join(problems)))
    return manifest, problems


def manifest_path(bundle_dir: os.PathLike[str] | str) -> Path:
    return Path(bundle_dir) / "manifest.json"
