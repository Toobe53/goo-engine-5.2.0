#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Goo Engine Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Execute the Goo Engine driver matrix against the current host.

The runner:

* loads and validates ``tools/driver_matrix/matrix.json``;
* detects the current host (or reads ``--host-json`` for offline planning);
* selects the matrix rows that match the host, recording why the others were
  skipped instead of silently ignoring them;
* runs the configured smoke commands (or ``--smoke-cmd`` overrides);
* writes ``report.json`` and ``report.md`` with logs next to them;
* exits non-zero when a required run fails.

``--dry-run`` prints the plan without executing anything. It is safe to run on
machines without a Goo binary, which is the normal case for CI lint jobs.

Usage::

    python tools/driver_matrix/run_matrix.py --list
    python tools/driver_matrix/run_matrix.py --dry-run
    python tools/driver_matrix/run_matrix.py --binary /path/to/goo --out-dir artifacts/driver-matrix
    python tools/driver_matrix/run_matrix.py --smoke-cmd "python -c 'print(1)'"
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import platform
import re
import shlex
import string
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MATRIX = SCRIPT_DIR / "matrix.json"
REPORT_SCHEMA_VERSION = "1.0"
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

ALLOWED_PLACEHOLDERS = {"goo_binary", "repo_root", "backend", "out_dir", "python", "entry_id"}
FAMILIES = ("windows", "linux", "macos")
ARCHES = ("x64", "arm64", "x86")
VENDORS = ("nvidia", "amd", "intel", "apple", "qualcomm", "arm", "unknown")
TIERS = ("gate", "nightly", "advisory")
HOSTED_LABEL_RE = re.compile(r"^(ubuntu|windows|macos)-(latest|\d)")

sys.path.insert(0, str(SCRIPT_DIR))

try:
    import detect as host_detect  # noqa: E402  (local module next to this script)
except Exception as _detect_error:  # pragma: no cover - broken checkout guard
    host_detect = None
    _DETECT_IMPORT_ERROR = _detect_error
else:
    _DETECT_IMPORT_ERROR = None


class MatrixError(Exception):
    """Raised for invalid matrix files or command templates."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt: datetime.datetime | None = None) -> str:
    return (dt or utc_now()).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as ex:
        raise MatrixError("cannot read %s: %s" % (path, ex)) from ex
    except json.JSONDecodeError as ex:
        raise MatrixError("invalid JSON in %s: %s" % (path, ex)) from ex


def version_tuple(value):
    if not value:
        return None
    match = re.search(r"\d+(?:\.\d+)*", str(value))
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split(".") if part.isdigit())


def compare_versions(host: str | None, minimum: str | None) -> str:
    host_t = version_tuple(host)
    min_t = version_tuple(minimum)
    if host_t is None or min_t is None:
        return "unknown"
    width = max(len(host_t), len(min_t))
    host_t = host_t + (0,) * (width - len(host_t))
    min_t = min_t + (0,) * (width - len(min_t))
    return "met" if host_t >= min_t else "below"


def physical_gpus(host: dict) -> list:
    return [
        gpu
        for gpu in host.get("gpus", [])
        if gpu.get("kind") not in ("virtual", "software") and not gpu.get("is_virtual") and not gpu.get("is_software")
    ]


def gpu_for_vendor(host: dict, vendor: str) -> dict | None:
    candidates = [gpu for gpu in physical_gpus(host) if gpu.get("vendor") == vendor]
    if not candidates:
        return None
    candidates.sort(key=lambda gpu: 0 if gpu.get("is_primary") else 1)
    return candidates[0]


def split_command(text: str) -> list:
    """Split a user supplied command line into argv without invoking a shell."""
    if os.name == "nt":
        parts = shlex.split(text, posix=False)
        cleaned = []
        for part in parts:
            if len(part) >= 2 and part[0] == part[-1] and part[0] in ("'", '"'):
                part = part[1:-1]
            cleaned.append(part)
        return cleaned
    return shlex.split(text)


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "unknown").strip("-")
    return cleaned or "unknown"


# ---------------------------------------------------------------------------
# Matrix validation
# ---------------------------------------------------------------------------


def _check_command_list(commands, where: str, errors: list):
    if not isinstance(commands, list) or not commands:
        errors.append("%s: commands must be a non-empty list" % where)
        return
    for index, command in enumerate(commands):
        if not isinstance(command, list) or not command:
            errors.append("%s: command %d must be a non-empty argv list" % (where, index))
            continue
        for part in command:
            if not isinstance(part, str) or not part:
                errors.append("%s: command %d contains a non-string or empty argument" % (where, index))
                break
        for part in command:
            if not isinstance(part, str):
                continue
            for placeholder in PLACEHOLDER_RE.findall(part):
                if placeholder not in ALLOWED_PLACEHOLDERS:
                    errors.append(
                        "%s: command %d uses unknown placeholder {%s} (allowed: %s)"
                        % (where, index, placeholder, ", ".join(sorted(ALLOWED_PLACEHOLDERS)))
                    )


def validate_matrix(doc: dict) -> tuple[list, list]:
    errors: list = []
    warnings: list = []
    if not isinstance(doc, dict):
        return ["matrix root must be a JSON object"], warnings
    for key in ("schema_version", "backends", "test_sets", "environments"):
        if key not in doc:
            errors.append("missing top-level key: %s" % key)
    if errors:
        return errors, warnings

    backends = doc.get("backends")
    if not isinstance(backends, dict) or not backends:
        errors.append("backends must be a non-empty object")
        backends = {}

    test_sets = doc.get("test_sets")
    if not isinstance(test_sets, dict) or not test_sets:
        errors.append("test_sets must be a non-empty object")
        test_sets = {}
    for name, test_set in test_sets.items():
        where = "test_sets.%s" % name
        if not isinstance(test_set, dict):
            errors.append("%s must be an object" % where)
            continue
        _check_command_list(test_set.get("commands"), where, errors)
        if "backend" in test_set and test_set["backend"] not in backends:
            errors.append("%s: backend %r is not declared in backends" % (where, test_set["backend"]))
        if "per_backend" in test_set and not isinstance(test_set["per_backend"], bool):
            errors.append("%s: per_backend must be a boolean" % where)
        if "backend" in test_set and test_set.get("per_backend"):
            errors.append("%s: backend and per_backend are mutually exclusive" % where)
        timeout = test_set.get("timeout_sec", 600)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            errors.append("%s: timeout_sec must be a positive number" % where)
        artifacts = test_set.get("artifacts", [])
        if not isinstance(artifacts, list) or any(not isinstance(item, str) or not item for item in artifacts):
            errors.append("%s: artifacts must be a list of non-empty strings" % where)

    environments = doc.get("environments")
    if not isinstance(environments, list) or not environments:
        errors.append("environments must be a non-empty list")
        environments = []
    seen_ids = set()
    for index, entry in enumerate(environments):
        where = "environments[%d]" % index
        if not isinstance(entry, dict):
            errors.append("%s must be an object" % where)
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            errors.append("%s: id must be a non-empty string" % where)
            continue
        where = "environments.%s" % entry_id
        if entry_id in seen_ids:
            errors.append("%s: duplicate environment id" % where)
        seen_ids.add(entry_id)

        os_info = entry.get("os")
        if not isinstance(os_info, dict):
            errors.append("%s: os must be an object" % where)
        else:
            if os_info.get("family") not in FAMILIES:
                errors.append("%s: os.family must be one of %s" % (where, ", ".join(FAMILIES)))
            if not str(os_info.get("release") or ""):
                errors.append("%s: os.release is required" % where)
            if os_info.get("arch") not in ARCHES:
                errors.append("%s: os.arch must be one of %s" % (where, ", ".join(ARCHES)))

        gpu = entry.get("gpu")
        if not isinstance(gpu, dict):
            errors.append("%s: gpu must be an object" % where)
        else:
            if gpu.get("vendor") not in VENDORS:
                errors.append("%s: gpu.vendor must be one of %s" % (where, ", ".join(VENDORS)))
            min_driver = gpu.get("min_driver")
            if not isinstance(min_driver, dict):
                errors.append("%s: gpu.min_driver must be an object" % where)
            else:
                for field in ("display", "scheme", "version"):
                    if not isinstance(min_driver.get(field), str) or not min_driver.get(field):
                        errors.append("%s: gpu.min_driver.%s must be a non-empty string" % (where, field))

        entry_backends = entry.get("backends")
        if not isinstance(entry_backends, list) or not entry_backends:
            errors.append("%s: backends must be a non-empty list" % where)
        else:
            for backend in entry_backends:
                if backend not in backends:
                    errors.append("%s: backend %r is not declared in backends" % (where, backend))

        entry_test_sets = entry.get("test_sets")
        if not isinstance(entry_test_sets, list) or not entry_test_sets:
            errors.append("%s: test_sets must be a non-empty list" % where)
        else:
            for test_set_name in entry_test_sets:
                if test_set_name not in test_sets:
                    errors.append("%s: unknown test set %r" % (where, test_set_name))
                    continue
                fixed_backend = test_sets[test_set_name].get("backend")
                if fixed_backend and isinstance(entry_backends, list) and fixed_backend not in entry_backends:
                    errors.append(
                        "%s: test set %r requires backend %r which is not listed in entry backends"
                        % (where, test_set_name, fixed_backend)
                    )

        labels = entry.get("runner_labels")
        if not isinstance(labels, list) or not labels or any(not isinstance(label, str) or not label for label in labels):
            errors.append("%s: runner_labels must be a non-empty list of strings" % where)
        else:
            if "self-hosted" not in labels:
                errors.append("%s: runner_labels must include self-hosted" % where)
            for label in labels:
                if HOSTED_LABEL_RE.match(label):
                    errors.append("%s: runner label %r is a GitHub-hosted image; use self-hosted labels" % (where, label))

        if not isinstance(entry.get("required"), bool):
            errors.append("%s: required must be a boolean" % where)
        if entry.get("tier") not in TIERS:
            errors.append("%s: tier must be one of %s" % (where, ", ".join(TIERS)))

        display_server = entry.get("display_server")
        if display_server is not None and not isinstance(display_server, str):
            errors.append("%s: display_server must be a string" % where)

    candidates = doc.get("binary_candidates")
    if candidates is not None and not isinstance(candidates, list):
        errors.append("binary_candidates must be a list")

    if not errors:
        covered = {(entry["os"]["family"], entry["os"]["release"]) for entry in environments}
        for family, release in (("windows", "10"), ("windows", "11"), ("linux", "22.04"), ("linux", "24.04"), ("macos", "14"), ("macos", "15")):
            if (family, release) not in covered:
                warnings.append("matrix has no environment for %s %s" % (family, release))
        vendors = {entry["gpu"]["vendor"] for entry in environments}
        for vendor in ("nvidia", "amd", "intel", "apple"):
            if vendor not in vendors:
                warnings.append("matrix has no environment for GPU vendor %s" % vendor)
    return errors, warnings


def load_matrix(path: Path) -> dict:
    doc = load_json(path)
    errors, warnings = validate_matrix(doc)
    for message in warnings:
        print("matrix warning: %s" % message, file=sys.stderr)
    if errors:
        for message in errors:
            print("matrix error: %s" % message, file=sys.stderr)
        raise MatrixError("%d validation error(s) in %s" % (len(errors), path))
    return doc


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def match_entry(entry: dict, host: dict) -> tuple[bool, list]:
    reasons = []
    host_os = (host.get("host") or {}).get("os") or {}
    entry_os = entry.get("os") or {}
    if host_os.get("family") != entry_os.get("family"):
        reasons.append("os family %r != %r" % (host_os.get("family"), entry_os.get("family")))
    elif str(host_os.get("release")) != str(entry_os.get("release")):
        reasons.append("os release %r != %r" % (host_os.get("release"), entry_os.get("release")))
    if (host.get("host") or {}).get("arch") != entry_os.get("arch"):
        reasons.append("arch %r != %r" % ((host.get("host") or {}).get("arch"), entry_os.get("arch")))

    vendor = (entry.get("gpu") or {}).get("vendor")
    if not any(gpu.get("vendor") == vendor for gpu in physical_gpus(host)):
        reasons.append("no physical GPU with vendor %r" % vendor)

    display_server = entry.get("display_server")
    host_display = host.get("display_server")
    if display_server and host_display and display_server not in ("any", host_display):
        reasons.append("display server %r != %r" % (host_display, display_server))
    return (not reasons), reasons


def entry_filtered(entry: dict, args) -> str | None:
    if args.entry and entry.get("id") not in args.entry:
        return "filtered by --entry"
    if args.tier and entry.get("tier") not in args.tier:
        return "filtered by --tier"
    if args.os_family and entry.get("os", {}).get("family") not in args.os_family:
        return "filtered by --os-family"
    if args.gpu_vendor and entry.get("gpu", {}).get("vendor") not in args.gpu_vendor:
        return "filtered by --gpu-vendor"
    if args.required_only and not entry.get("required"):
        return "filtered by --required-only"
    if args.backend and not (set(entry.get("backends") or []) & set(args.backend)):
        return "filtered by --backend"
    return None


def driver_conformance(entry: dict, host: dict) -> dict:
    min_driver = (entry.get("gpu") or {}).get("min_driver") or {}
    scheme = min_driver.get("scheme")
    vendor = (entry.get("gpu") or {}).get("vendor")
    host_version = None
    host_raw = None
    source = None
    minimum = min_driver.get("version")
    gpu = gpu_for_vendor(host, vendor)
    drivers = host.get("drivers") or {}
    host_os = (host.get("host") or {}).get("os") or {}

    if scheme == "nvidia-windows":
        if gpu:
            host_version = gpu.get("driver_normalized")
            host_raw = gpu.get("driver_version")
            source = gpu.get("name")
        minimum = min_driver.get("version")
    elif scheme == "nvidia-linux":
        if gpu:
            host_version = gpu.get("driver_normalized") or gpu.get("driver_version")
            host_raw = gpu.get("driver_version")
            source = gpu.get("name")
        if not host_version:
            host_version = (drivers.get("nvidia") or {}).get("version")
            source = "/proc/driver/nvidia/version"
    elif scheme in ("amd-windows-raw", "intel-windows-raw"):
        if gpu:
            host_version = gpu.get("driver_normalized") or gpu.get("driver_version")
            host_raw = gpu.get("driver_version")
            source = gpu.get("name")
        minimum = min_driver.get("raw_version") or min_driver.get("version")
    elif scheme == "mesa":
        host_version = (drivers.get("mesa") or {}).get("version")
        if not host_version and gpu:
            host_version = gpu.get("driver_normalized")
        source = "glxinfo/vulkaninfo (Mesa)"
        if not host_version and gpu:
            host_raw = gpu.get("driver_version")
    elif scheme == "apple-os":
        host_version = host_os.get("version")
        host_raw = host_os.get("build")
        source = "sw_vers"
        minimum = min_driver.get("version")
    elif scheme == "windows-generic":
        if gpu:
            host_version = gpu.get("driver_version")
        source = "Win32_VideoController"
    else:
        source = "unsupported scheme %r" % scheme

    status = compare_versions(host_version, minimum)
    return {
        "entry_id": entry.get("id"),
        "vendor": vendor,
        "scheme": scheme,
        "host_version": host_version,
        "host_raw_version": host_raw,
        "minimum_version": minimum,
        "minimum_display": min_driver.get("display"),
        "status": status,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Binary and command expansion
# ---------------------------------------------------------------------------


def resolve_binary(args, matrix: dict, repo_root: Path) -> tuple[Path | None, list]:
    checked = []
    explicit = args.binary or os.environ.get("GOO_BINARY")
    if explicit:
        path = Path(explicit).expanduser()
        checked.append(str(path))
        if path.is_file():
            return path.resolve(), checked
        print("warning: Goo binary %s does not exist" % path, file=sys.stderr)
        return None, checked

    candidates = matrix.get("binary_candidates") or []
    for candidate in candidates:
        expanded = candidate.replace("{repo_root}", str(repo_root))
        path = Path(expanded)
        checked.append(str(path))
        if path.is_file():
            return path.resolve(), checked
    return None, checked


def mapping_for(repo_root: Path, binary: Path | None, out_dir: Path, entry: dict, backend: str | None) -> dict:
    return {
        "goo_binary": str(binary) if binary else "<goo-binary-not-found>",
        "repo_root": str(repo_root),
        "backend": backend or "",
        "out_dir": str(out_dir),
        "python": sys.executable,
        "entry_id": entry.get("id", "unknown"),
    }


def expand_command(template: str, mapping: dict) -> str:
    for placeholder in PLACEHOLDER_RE.findall(template):
        if placeholder not in mapping:
            raise MatrixError("unknown placeholder {%s} in %r" % (placeholder, template))
        template = template.replace("{%s}" % placeholder, mapping[placeholder])
    return template


def build_plan(selected: list, matrix: dict, args, repo_root: Path, binary: Path | None, out_dir: Path) -> list:
    runs = []
    if args.smoke_cmd:
        templates = [split_command(text) for text in args.smoke_cmd]
        for entry in selected:
            backends = [backend for backend in entry.get("backends", []) if not args.backend or backend in args.backend]
            for backend in backends:
                mapping = mapping_for(repo_root, binary, out_dir, entry, backend)
                commands = [[expand_command(part, mapping) for part in command] for command in templates]
                needs_binary = any("{goo_binary}" in part for command in templates for part in command)
                runs.append(
                    {
                        "entry_id": entry["id"],
                        "test_set": "custom-smoke",
                        "backend": backend,
                        "commands": commands,
                        "command_text": " && ".join(" ".join(command) for command in commands),
                        "timeout_sec": args.timeout or 900,
                        "required": bool(entry.get("required")),
                        "artifacts": [],
                        "needs_binary": needs_binary,
                    }
                )
        return runs

    for entry in selected:
        mapping_base = mapping_for(repo_root, binary, out_dir, entry, None)
        for test_set_name in entry.get("test_sets", []):
            if args.test_set and test_set_name not in args.test_set:
                continue
            test_set = matrix["test_sets"][test_set_name]
            if test_set.get("backend"):
                if args.backend and test_set["backend"] not in args.backend:
                    continue
                backends = [test_set["backend"]]
            elif test_set.get("per_backend"):
                backends = [
                    backend for backend in entry.get("backends", []) if not args.backend or backend in args.backend
                ]
            else:
                backends = [None]
            for backend in backends:
                mapping = dict(mapping_base)
                mapping["backend"] = backend or ""
                commands = [
                    [expand_command(part, mapping) for part in command] for command in test_set["commands"]
                ]
                artifacts = [expand_command(pattern, mapping) for pattern in test_set.get("artifacts", [])]
                needs_binary = any(
                    "{goo_binary}" in part for command in test_set["commands"] for part in command
                )
                timeout = args.timeout or test_set.get("timeout_sec") or 600
                runs.append(
                    {
                        "entry_id": entry["id"],
                        "test_set": test_set_name,
                        "backend": backend,
                        "commands": commands,
                        "command_text": " && ".join(" ".join(command) for command in commands),
                        "timeout_sec": timeout,
                        "required": bool(entry.get("required") and test_set.get("required", True)),
                        "artifacts": artifacts,
                        "needs_binary": needs_binary,
                    }
                )
    return runs


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_one(run: dict, repo_root: Path, env: dict, logs_dir: Path, binary: Path | None, allow_missing_binary: bool) -> dict:
    started = utc_now()
    started_monotonic = time.monotonic()
    result = {
        "entry_id": run["entry_id"],
        "test_set": run["test_set"],
        "backend": run["backend"],
        "command": run["command_text"],
        "required": run["required"],
        "status": "passed",
        "reason": None,
        "exit_code": None,
        "duration_sec": 0.0,
        "started_at": iso(started),
        "finished_at": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "log": None,
        "missing_artifacts": [],
    }
    output_chunks = []
    stderr_chunks = []
    if run.get("needs_binary") and binary is None:
        if allow_missing_binary:
            result["status"] = "skipped"
            result["reason"] = "missing-binary (allowed by --allow-missing-binary)"
        else:
            result["status"] = "error"
            result["reason"] = "missing-binary"
    else:
        for command in run["commands"]:
            output_chunks.append("$ %s" % " ".join(command))
            try:
                proc = subprocess.run(
                    command,
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=run["timeout_sec"],
                    env=env,
                )
            except subprocess.TimeoutExpired as ex:
                result["status"] = "failed"
                result["reason"] = "timeout after %ss" % run["timeout_sec"]
                partial_out = ex.stdout if isinstance(ex.stdout, str) else (ex.stdout or b"").decode("utf-8", "replace")
                partial_err = ex.stderr if isinstance(ex.stderr, str) else (ex.stderr or b"").decode("utf-8", "replace")
                output_chunks.append(partial_out or "")
                output_chunks.append(partial_err or "")
                stderr_chunks.append(partial_err or "")
                break
            except OSError as ex:
                result["status"] = "error"
                result["reason"] = "failed to start command: %s" % ex
                output_chunks.append(str(ex))
                stderr_chunks.append(str(ex))
                break
            output_chunks.append(proc.stdout or "")
            output_chunks.append(proc.stderr or "")
            if proc.stderr:
                stderr_chunks.append(proc.stderr)
            result["exit_code"] = proc.returncode
            if proc.returncode != 0:
                result["status"] = "failed"
                result["reason"] = "exit code %d" % proc.returncode
                break

        if result["status"] == "passed" and run.get("artifacts"):
            missing = []
            for pattern in run["artifacts"]:
                matches = glob.glob(pattern)
                if not matches:
                    missing.append(pattern)
            if missing:
                result["status"] = "failed"
                result["reason"] = "missing expected artifact(s)"
                result["missing_artifacts"] = missing

    combined = "\n".join(chunk for chunk in output_chunks if chunk)
    result["stdout_tail"] = combined[-4000:]
    result["stderr_tail"] = "\n".join(chunk for chunk in stderr_chunks if chunk)[-4000:]
    result["duration_sec"] = round(time.monotonic() - started_monotonic, 3)
    result["finished_at"] = iso()

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_name = "%s__%s__%s.log" % (
        safe_name(run["entry_id"]),
        safe_name(run["test_set"]),
        safe_name(run["backend"] or "default"),
    )
    log_path = logs_dir / log_name
    try:
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("command: %s\n" % run["command_text"])
            fh.write("status: %s\nreason: %s\nexit_code: %s\n\n" % (result["status"], result["reason"], result["exit_code"]))
            fh.write(combined + "\n")
        result["log"] = str(log_path)
    except OSError as ex:
        result["reason"] = (result["reason"] or "") + " (log write failed: %s)" % ex
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_markdown(report: dict) -> str:
    host = report.get("host") or {}
    host_os = (host.get("host") or {}).get("os") or {}
    summary = report.get("summary") or {}
    lines = []
    lines.append("# Goo Engine 驱动矩阵执行报告")
    lines.append("")
    lines.append("- 生成时间：%s" % report.get("generated_at"))
    lines.append("- 矩阵文件：`%s`（schema %s）" % (report["matrix"].get("path"), report["matrix"].get("schema_version")))
    lines.append("- 项目：%s %s（%s）" % (report["matrix"].get("project", {}).get("name"), report["matrix"].get("project", {}).get("version"), report["matrix"].get("project", {}).get("branch")))
    lines.append("- 判定：**%s**" % summary.get("verdict", "unknown").upper())
    lines.append("")

    lines.append("## 主机")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("| --- | --- |")
    lines.append("| 系统 | %s %s（%s） |" % (host_os.get("name", "unknown"), host_os.get("version", ""), host_os.get("arch", "")))
    lines.append("| 显示服务 | %s |" % (host.get("display_server") or "unknown"))
    lines.append("| CI | %s |" % ((host.get("ci") or {}).get("provider") or "not detected"))
    gpu_text = "<br>".join(
        "%s（%s，%s，驱动 %s）"
        % (gpu.get("name"), gpu.get("vendor"), gpu.get("kind"), gpu.get("driver_version") or "unknown")
        for gpu in host.get("gpus", [])
    ) or "未检测到"
    lines.append("| GPU | %s |" % gpu_text)
    backends = host.get("backends") or {}
    backend_text = "<br>".join(
        "%s: %s%s"
        % (
            name,
            "available" if data.get("available") else ("unavailable" if data.get("available") is False else "unknown"),
            "（%s）" % data.get("version") if data.get("version") else "",
        )
        for name, data in backends.items()
    ) or "未知"
    lines.append("| 后端 | %s |" % backend_text)
    lines.append("")

    lines.append("## 矩阵筛选")
    lines.append("")
    matched = report.get("selection", {}).get("matched", [])
    lines.append("匹配条目：%s" % (", ".join("`%s`" % item for item in matched) if matched else "无"))
    lines.append("")
    skipped = report.get("selection", {}).get("skipped", [])
    if skipped:
        lines.append("| 跳过条目 | 级别 | 原因 |")
        lines.append("| --- | --- | --- |")
        for item in skipped:
            lines.append("| `%s` | %s | %s |" % (item.get("id"), item.get("tier"), "; ".join(item.get("reasons") or [])))
        lines.append("")

    conformance = report.get("driver_conformance", [])
    if conformance:
        lines.append("## 驱动符合性")
        lines.append("")
        lines.append("| 条目 | 主机驱动 | 最低要求 | 结论 |")
        lines.append("| --- | --- | --- | --- |")
        for item in conformance:
            lines.append(
                "| `%s` | %s | %s | %s |"
                % (
                    item.get("entry_id"),
                    item.get("host_version") or "unknown",
                    item.get("minimum_display") or item.get("minimum_version"),
                    item.get("status"),
                )
            )
        lines.append("")

    lines.append("## 执行结果")
    lines.append("")
    runs = report.get("runs", [])
    if runs:
        lines.append("| 条目 | 测试集 | 后端 | 必需 | 状态 | 退出码 | 耗时(s) |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for run in runs:
            lines.append(
                "| `%s` | %s | %s | %s | **%s** | %s | %s |"
                % (
                    run["entry_id"],
                    run["test_set"],
                    run.get("backend") or "-",
                    "yes" if run.get("required") else "no",
                    run["status"],
                    run.get("exit_code") if run.get("exit_code") is not None else "-",
                    run.get("duration_sec"),
                )
            )
        lines.append("")
    else:
        lines.append("未执行任何测试（没有匹配条目或全部被过滤）。")
        lines.append("")

    failures = [run for run in runs if run["status"] in ("failed", "error")]
    if failures:
        lines.append("## 失败详情")
        lines.append("")
        for run in failures:
            lines.append("### `%s` / %s / %s" % (run["entry_id"], run["test_set"], run.get("backend") or "-"))
            lines.append("")
            lines.append("- 原因：%s" % (run.get("reason") or "unknown"))
            lines.append("- 命令：`%s`" % run.get("command"))
            if run.get("missing_artifacts"):
                lines.append("- 缺失产物：%s" % ", ".join("`%s`" % item for item in run["missing_artifacts"]))
            if run.get("log"):
                lines.append("- 完整日志：`%s`" % run["log"])
            tail = (run.get("stdout_tail") or "").strip().splitlines()[-25:]
            if tail:
                lines.append("")
                lines.append("```text")
                lines.extend(tail)
                lines.append("```")
            lines.append("")

    lines.append("## 说明")
    lines.append("")
    lines.append("- 本报告只代表当前主机；矩阵中未匹配的条目必须由对应的自托管 GPU runner 执行。")
    lines.append("- GitHub 托管 runner 不提供可用 GPU，不能代替本矩阵（见 `docs/production/driver-matrix.md`）。")
    lines.append("- 驱动符合性默认只记录不阻断；使用 `--require-min-driver` 可将低于基线的驱动视为失败。")
    lines.append("- 任何 failed/error 都会让退出码非零；`--strict` 会额外把 skipped（如缺少二进制）也视为失败。")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_reports(report: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    md_path = out_dir / "report.md"
    tmp_json = json_path.with_suffix(".json.tmp")
    with open(tmp_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp_json, json_path)
    tmp_md = md_path.with_suffix(".md.tmp")
    with open(tmp_md, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    os.replace(tmp_md, md_path)
    return json_path, md_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def print_plan(plan: list, matched: list, skipped: list, binary: Path | None, checked: list, host: dict) -> None:
    host_os = (host.get("host") or {}).get("os") or {}
    print("host: %s %s %s | display=%s" % (host_os.get("family"), host_os.get("release"), host_os.get("arch"), host.get("display_server")))
    for gpu in physical_gpus(host):
        print("gpu: %s (%s, %s, driver=%s)" % (gpu.get("name"), gpu.get("vendor"), gpu.get("kind"), gpu.get("driver_version")))
    print("matched: %d environment(s) -> %s" % (len(matched), ", ".join(entry["id"] for entry in matched) or "none"))
    if skipped:
        print("skipped: %d environment(s)" % len(skipped))
    if binary:
        print("goo binary: %s" % binary)
    else:
        print("goo binary: not found (set --binary or GOO_BINARY)")
        if checked:
            print("  checked: %s" % ", ".join(checked))
    print("plan: %d run(s)" % len(plan))
    for index, run in enumerate(plan, start=1):
        print(
            "  [%02d] %s / %s / %s%s (timeout %ss)"
            % (
                index,
                run["entry_id"],
                run["test_set"],
                run.get("backend") or "-",
                " [required]" if run.get("required") else " [optional]",
                run["timeout_sec"],
            )
        )
        for command in run["commands"]:
            print("       $ %s" % " ".join(command))
        if run.get("needs_binary") and binary is None:
            print("       ! would fail: missing Goo binary")
    print("dry-run: nothing executed")


def print_list(matrix: dict) -> None:
    print("%-32s %-8s %-9s %-8s %-8s %-7s %s" % ("id", "tier", "required", "os", "release", "arch", "gpu/backends"))
    for entry in matrix.get("environments", []):
        print(
            "%-32s %-8s %-9s %-8s %-8s %-7s %s %s"
            % (
                entry["id"],
                entry.get("tier"),
                "yes" if entry.get("required") else "no",
                entry["os"]["family"],
                entry["os"]["release"],
                entry["os"]["arch"],
                entry["gpu"]["vendor"],
                ",".join(entry["backends"]),
            )
        )
    print("")
    print("total: %d environment(s)" % len(matrix.get("environments", [])))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select and execute Goo Engine driver-matrix rows on the current host.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--matrix", default=str(DEFAULT_MATRIX), help="Path to matrix.json.")
    parser.add_argument("--binary", help="Path to the Goo/Blender binary (default: GOO_BINARY or matrix candidates).")
    parser.add_argument("--out-dir", help="Report directory (default: driver_matrix_reports/<UTC timestamp>).")
    parser.add_argument("--smoke-cmd", action="append", default=[], metavar="CMD", help="Override the test-set commands (repeatable, runs once per backend).")
    parser.add_argument("--test-set", action="append", default=[], metavar="NAME", help="Only run these test sets (repeatable).")
    parser.add_argument("--entry", action="append", default=[], metavar="ID", help="Only consider these environment ids (repeatable).")
    parser.add_argument("--tier", action="append", default=[], metavar="TIER", help="Only consider these tiers (gate/nightly/advisory).")
    parser.add_argument("--os-family", action="append", default=[], metavar="NAME", help="Only consider these OS families.")
    parser.add_argument("--gpu-vendor", action="append", default=[], metavar="NAME", help="Only consider these GPU vendors.")
    parser.add_argument("--backend", action="append", default=[], metavar="NAME", help="Only run these backends.")
    parser.add_argument("--required-only", action="store_true", help="Only consider required (gate) entries.")
    parser.add_argument("--host-json", help="Read a detect.py JSON document instead of probing the host (offline planning).")
    parser.add_argument("--timeout", type=float, default=0.0, help="Override per-command timeout in seconds (0 = use matrix).")
    parser.add_argument("--allow-missing-binary", action="store_true", help="Skip runs that need the binary instead of failing.")
    parser.add_argument("--allow-empty", action="store_true", help="Exit 0 when no matrix row matches this host.")
    parser.add_argument("--require-min-driver", action="store_true", help="Treat drivers below the matrix baseline as failures.")
    parser.add_argument("--strict", action="store_true", help="Also fail when runs were skipped (e.g. --allow-missing-binary).")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without executing or writing reports.")
    parser.add_argument("--list", action="store_true", help="List matrix environments and exit.")
    parser.add_argument("--validate-only", action="store_true", help="Validate the matrix file and exit.")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    matrix_path = Path(args.matrix).expanduser()
    try:
        matrix = load_matrix(matrix_path)
    except MatrixError as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 2

    if args.list:
        print_list(matrix)
        return 0
    if args.validate_only:
        print("matrix ok: %s (%d environments, %d test sets)" % (matrix_path, len(matrix["environments"]), len(matrix["test_sets"])))
        return 0

    if args.host_json:
        try:
            host = load_json(Path(args.host_json).expanduser())
        except MatrixError as ex:
            print("error: %s" % ex, file=sys.stderr)
            return 2
        if not isinstance(host, dict):
            print("error: --host-json must contain a JSON object", file=sys.stderr)
            return 2
    else:
        if host_detect is None:
            print("error: cannot import detect.py next to run_matrix.py: %s" % _DETECT_IMPORT_ERROR, file=sys.stderr)
            return 2
        # Let the host detector see Blender's bundled Vulkan loader before it
        # runs; the binary path is otherwise only resolved further below.
        explicit_binary = args.binary or os.environ.get("GOO_BINARY")
        if explicit_binary:
            os.environ["GOO_DRIVER_MATRIX_BINARY"] = str(Path(explicit_binary).expanduser().resolve())
        host = host_detect.detect_host()

    repo_root = matrix_path.resolve().parent.parent.parent
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else Path.cwd() / "driver_matrix_reports" / utc_now().strftime("%Y%m%dT%H%M%SZ")
    binary, checked = resolve_binary(args, matrix, repo_root)

    matched = []
    skipped = []
    for entry in matrix["environments"]:
        filter_reason = entry_filtered(entry, args)
        if filter_reason:
            skipped.append({"id": entry["id"], "tier": entry.get("tier"), "required": entry.get("required"), "reasons": [filter_reason]})
            continue
        is_match, reasons = match_entry(entry, host)
        if is_match:
            matched.append(entry)
        else:
            skipped.append({"id": entry["id"], "tier": entry.get("tier"), "required": entry.get("required"), "reasons": reasons})

    conformance = [driver_conformance(entry, host) for entry in matched]
    try:
        plan = build_plan(matched, matrix, args, repo_root, binary, out_dir)
    except MatrixError as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 2

    if args.dry_run:
        print_plan(plan, matched, skipped, binary, checked, host)
        if not matched:
            print("warning: no matrix environment matches this host")
        return 0

    if not matched:
        print("error: no matrix environment matches this host (use --allow-empty to ignore)", file=sys.stderr)
        print("hint: run detect.py and compare with --list", file=sys.stderr)
        return 2 if not args.allow_empty else 0

    if not plan:
        print("error: matched entries produced no runnable test sets (check --test-set/--backend)", file=sys.stderr)
        return 2 if not args.allow_empty else 0

    logs_dir = out_dir / "logs"
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["GOO_DRIVER_MATRIX"] = "1"

    runs = []
    for index, run in enumerate(plan, start=1):
        env["GOO_DRIVER_MATRIX_ENTRY"] = run["entry_id"]
        env["GOO_DRIVER_MATRIX_BACKEND"] = run.get("backend") or ""
        if binary:
            env["GOO_BINARY"] = str(binary)
        print("[%02d/%02d] %s / %s / %s" % (index, len(plan), run["entry_id"], run["test_set"], run.get("backend") or "-"))
        result = run_one(run, repo_root, env, logs_dir, binary, args.allow_missing_binary)
        runs.append(result)
        print("        -> %s (%s) in %.2fs" % (result["status"], result.get("reason") or "ok", result["duration_sec"]))

    summary = {
        "runs_total": len(runs),
        "passed": sum(1 for run in runs if run["status"] == "passed"),
        "failed": sum(1 for run in runs if run["status"] == "failed"),
        "error": sum(1 for run in runs if run["status"] == "error"),
        "skipped": sum(1 for run in runs if run["status"] == "skipped"),
        "required_runs": sum(1 for run in runs if run["required"]),
        "required_failures": sum(1 for run in runs if run["required"] and run["status"] in ("failed", "error")),
        "optional_failures": sum(1 for run in runs if not run["required"] and run["status"] in ("failed", "error")),
        "driver_failures": sum(1 for item in conformance if item["status"] == "below") if args.require_min_driver else 0,
    }
    verdict = "pass"
    if summary["failed"] or summary["error"] or (args.strict and summary["skipped"]):
        verdict = "fail"
    if args.require_min_driver and summary["driver_failures"]:
        verdict = "fail"
    summary["verdict"] = verdict

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": iso(),
        "matrix": {
            "path": str(matrix_path),
            "schema_version": matrix.get("schema_version"),
            "project": matrix.get("project", {}),
            "environments_total": len(matrix["environments"]),
        },
        "host": host,
        "selection": {"matched": [entry["id"] for entry in matched], "skipped": skipped},
        "driver_conformance": conformance,
        "environment": {
            "repo_root": str(repo_root),
            "out_dir": str(out_dir),
            "goo_binary": str(binary) if binary else None,
            "binary_candidates_checked": checked,
            "python": sys.version,
            "platform": platform.platform(),
        },
        "runs": runs,
        "summary": summary,
    }
    json_path, md_path = write_reports(report, out_dir)

    print("")
    print("summary: %d run(s), %d passed, %d failed, %d error, %d skipped" % (
        summary["runs_total"], summary["passed"], summary["failed"], summary["error"], summary["skipped"]))
    print("required failures: %d | optional failures: %d | verdict: %s" % (
        summary["required_failures"], summary["optional_failures"], verdict.upper()))
    if summary["driver_failures"]:
        print("driver baseline failures: %d" % summary["driver_failures"])
    print("report: %s" % json_path)
    print("report: %s" % md_path)
    if verdict != "pass":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
