"""Render a bundle manifest as human-readable Markdown or structured JSON.

The report is deliberately conservative: it reports what the bundle observed and
explicitly says when a verdict cannot be inferred. It does not symbolize dumps.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from . import SCHEMA_VERSION, __version__
from .bundle import load_manifest
from .util import human_bytes, utc_iso

REPORT_SCHEMA = "crashmon.report/1"

CRASH_CATEGORIES = {"crash", "exception", "timeout"}
ABNORMAL_CATEGORIES = {
    "crash",
    "exception",
    "timeout",
    "error",
    "spawn-error",
    "signal",
    "killed",
    "terminated",
    "interrupted",
}

CATEGORY_LABELS = {
    "success": "CLEAN EXIT",
    "crash": "CRASH",
    "exception": "CRASH SUSPECTED",
    "timeout": "TIMEOUT",
    "error": "ABNORMAL EXIT",
    "spawn-error": "FAILED TO START",
    "signal": "ABNORMAL SIGNAL",
    "killed": "KILLED",
    "terminated": "TERMINATED",
    "interrupted": "INTERRUPTED",
    "unknown": "UNKNOWN",
}


def summarize(manifest: dict[str, Any]) -> dict[str, Any]:
    run = manifest.get("run")
    exit_info = (run or {}).get("exit") if isinstance(run, dict) else None
    category = (exit_info or {}).get("category") if isinstance(exit_info, dict) else None
    if not isinstance(run, dict):
        verdict = "no-run-data"
        label = "NO RUN DATA"
    else:
        category = category or "unknown"
        label = CATEGORY_LABELS.get(category, category.upper())
        verdict = "crash" if category in CRASH_CATEGORIES else (
            "abnormal" if category in ABNORMAL_CATEGORIES else "clean" if category == "success" else "unknown"
        )
    artifacts = manifest.get("crash_artifacts") or []
    copied = [item for item in artifacts if isinstance(item, dict) and item.get("copied")]
    logs = manifest.get("logs") or []
    return {
        "verdict": verdict,
        "label": label,
        "crash_suspected": verdict in ("crash", "abnormal"),
        "exit_category": category,
        "exit": exit_info if isinstance(exit_info, dict) else None,
        "log_count": len(logs),
        "artifact_count": len(artifacts),
        "copied_artifact_count": len(copied),
        "retained_logs": any(isinstance(item, dict) and item.get("retained") for item in logs),
        "raw_logs_included": (manifest.get("privacy") or {}).get("raw_logs_included"),
        "upload_requested": (manifest.get("upload") or {}).get("requested", False),
        "network_performed": (manifest.get("upload") or {}).get("network_performed", False),
    }


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def _short_sha(value: Any) -> str:
    text = str(value or "")
    return text[:12] + "..." if len(text) > 12 else text or "n/a"


def _fence(text: str, dialect: str = "") -> str:
    longest = 0
    for line in text.splitlines():
        if line.startswith("`"):
            longest = max(longest, len(line) - len(line.lstrip("`")))
    marker = "`" * max(3, longest + 1)
    return "{}{}\n{}\n{}".format(marker, dialect, text, marker)


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return lines


def _is_blank(value: Any) -> bool:
    return value is None or value == ""


def _exit_section(summary: dict[str, Any], lines: list[str]) -> None:
    exit_info = summary.get("exit")
    lines.extend(["## Exit status", ""])
    if not exit_info:
        lines.extend(["No exit status was recorded (this bundle was collected, not supervised).", ""])
        return
    rows = [
        ["Category", exit_info.get("category")],
        ["Description", exit_info.get("description")],
        ["Raw return code", exit_info.get("raw")],
        ["Windows status", exit_info.get("code_hex")],
        ["Windows exception", (exit_info.get("windows_exception") or {}).get("name") if isinstance(exit_info.get("windows_exception"), dict) else None],
        ["POSIX signal", exit_info.get("signal")],
        ["Signal name", exit_info.get("signal_name")],
        ["Timed out", exit_info.get("timed_out")],
    ]
    # Filter out rows with no value to reduce noise.
    lines.extend(_table(["Field", "Value"], [row for row in rows if not _is_blank(row[1])]))
    lines.append("")


def _run_section(run: Optional[dict[str, Any]], lines: list[str]) -> None:
    if not isinstance(run, dict):
        return
    lines.extend(["## Run", ""])
    rows = [
        ["Started (UTC)", run.get("started_utc")],
        ["Ended (UTC)", run.get("ended_utc")],
        ["Duration", "{} s".format(run.get("duration_s")) if run.get("duration_s") is not None else None],
        ["Timeout", "{} s".format(run.get("timeout_seconds")) if run.get("timeout_seconds") else "none"],
        ["Working directory", run.get("cwd")],
        ["Spawn error", run.get("spawn_error")],
    ]
    lines.extend(_table(["Field", "Value"], [row for row in rows if not _is_blank(row[1])]))
    command = run.get("command") or []
    if command:
        lines.extend(["", "Command:", "", _fence(" ".join(str(part) for part in command), "text")])
    lines.append("")


def _artifacts_section(artifacts: list[dict[str, Any]], lines: list[str]) -> None:
    lines.extend(["## Crash artifacts", ""])
    if not artifacts:
        lines.extend(
            [
                "No crash artifacts were found. This is **not** proof that no crash occurred; see limitations.",
                "",
            ]
        )
        return
    rows = []
    for entry in artifacts:
        rows.append(
            [
                entry.get("kind"),
                entry.get("source"),
                entry.get("relative_path") or "not copied",
                human_bytes(entry.get("size_bytes")),
                _short_sha(entry.get("sha256")),
                "yes" if entry.get("text_redacted") else ("n/a" if not entry.get("copied") else "no"),
                entry.get("note"),
            ]
        )
    lines.extend(
        _table(["Kind", "Source", "Bundle path", "Size", "SHA-256", "Text redacted", "Note"], rows)
    )
    lines.append("")


def _logs_section(logs: list[dict[str, Any]], lines: list[str], tail_lines: int) -> None:
    lines.extend(["## Log tails", ""])
    if not logs:
        lines.extend(["No logs were captured.", ""])
        return
    for entry in logs:
        stream = entry.get("stream") or "log"
        mode = "retained" if entry.get("retained") else "tail only"
        lines.extend(
            [
                "### {} ({})".format(stream, mode),
                "",
                "- Bundle file: `{}`".format(entry.get("relative_path") or "not stored"),
                "- Size: {} · SHA-256: `{}`".format(
                    human_bytes(entry.get("size_bytes")), entry.get("sha256") or "n/a"
                ),
            ]
        )
        rotation = entry.get("rotation") or {}
        if rotation:
            lines.append(
                "- Rotation: max {} · backups {} · rotated: {}".format(
                    human_bytes(rotation.get("max_bytes")), rotation.get("backups"), rotation.get("rotated")
                )
            )
        tail = entry.get("tail") or []
        if tail_lines > 0:
            shown = tail[-tail_lines:]
            lines.extend(["", _fence("\n".join(str(line) for line in shown), "text"), ""])
        if entry.get("errors"):
            lines.extend(["Reader errors: {}".format("; ".join(str(item) for item in entry["errors"])), ""])
    lines.append("")


def _system_section(manifest: dict[str, Any], lines: list[str]) -> None:
    lines.extend(["## System, GPU, and Blender", ""])
    system = manifest.get("system") or {}
    os_info = system.get("os") or {}
    cpu = system.get("cpu") or {}
    memory = system.get("memory") or {}
    python = system.get("python") or {}
    rows = [
        ["OS", os_info.get("name")],
        ["OS release", os_info.get("release")],
        ["OS version", os_info.get("version")],
        ["Platform", os_info.get("platform")],
        ["Architecture", system.get("arch")],
        ["CPU", cpu.get("model")],
        ["Logical cores", cpu.get("logical_cores")],
        ["Memory", human_bytes(memory.get("total_bytes")) if isinstance(memory, dict) else None],
        ["Python", python.get("version") if isinstance(python, dict) else system.get("python")],
    ]
    lines.extend(_table(["System", "Value"], [row for row in rows if not _is_blank(row[1])]))

    gpu = manifest.get("gpu") or {}
    lines.extend(["", "### GPU", ""])
    adapters = gpu.get("adapters") or gpu.get("devices") or []
    if adapters:
        gpu_rows = []
        for device in adapters:
            if not isinstance(device, dict):
                gpu_rows.append([str(device)])
                continue
            gpu_rows.append(
                [
                    device.get("name") or device.get("device"),
                    device.get("driver_version") or device.get("driver"),
                    device.get("vendor"),
                    device.get("source"),
                ]
            )
        lines.extend(_table(["Device", "Driver", "Vendor", "Source"], gpu_rows))
    elif gpu.get("skipped"):
        lines.append("GPU probe skipped: {}".format(gpu.get("reason") or "not requested"))
    else:
        lines.append("GPU probe produced no device records: {}".format(gpu.get("error") or "unavailable"))
    lines.append("")

    blender = manifest.get("blender") or {}
    lines.extend(["### Blender", ""])
    blender_rows = [
        ["Executable", blender.get("path")],
        ["Exists", blender.get("exists")],
        ["Version", blender.get("version")],
        ["Build hash", blender.get("build_hash")],
        ["Build date", blender.get("build_date")],
        ["Build platform", blender.get("build_platform")],
        ["Probe error", blender.get("error")],
    ]
    lines.extend(_table(["Field", "Value"], [row for row in blender_rows if not _is_blank(row[1])]))
    lines.append("")


def _platform_section(manifest: dict[str, Any], lines: list[str]) -> None:
    platform_info = manifest.get("platform") or {}
    if not platform_info:
        return
    lines.extend(["## Platform collection capabilities", ""])
    lines.extend(["```json", _json_pretty(platform_info), "```", ""])


def _environment_section(manifest: dict[str, Any], lines: list[str]) -> None:
    environment = manifest.get("environment") or {}
    values = environment.get("values") or {}
    lines.extend(["## Environment (allowlist)", ""])
    lines.append(
        "Collected {} allowlisted variable(s); {} other variable(s) were intentionally not captured.".format(
            environment.get("present_count", len(values)), environment.get("excluded_key_count", 0)
        )
    )
    lines.append("")
    if values:
        lines.extend(_table(["Variable", "Value"], [[key, value] for key, value in sorted(values.items())]))
        lines.append("")


def _privacy_section(manifest: dict[str, Any], summary: dict[str, Any], lines: list[str]) -> None:
    privacy = manifest.get("privacy") or {}
    redaction = privacy.get("redaction") or {}
    lines.extend(["## Privacy and retention", ""])
    rows = [
        ["Redaction level", privacy.get("level")],
        ["Redaction applied", redaction.get("applied")],
        ["Replacements", redaction.get("replacements")],
        ["Raw logs included", privacy.get("raw_logs_included")],
        ["Environment scope", privacy.get("environment_scope")],
        ["Network", privacy.get("network")],
    ]
    lines.extend(_table(["Field", "Value"], [row for row in rows if row[1] is not None]))
    lines.extend(
        [
            "",
            "Retention: this prototype does not delete bundles automatically. Raw logs are retained unless "
            "`--no-raw-logs` was passed. Text is redacted best-effort; binary dumps are copied verbatim and may "
            "contain process memory. Delete bundles according to your own data policy.",
            "",
        ]
    )


def _upload_section(manifest: dict[str, Any], lines: list[str]) -> None:
    upload = manifest.get("upload") or {}
    lines.extend(["## Upload boundary", ""])
    rows = [
        ["Requested", upload.get("requested")],
        ["Consent", upload.get("consent")],
        ["Mode", upload.get("mode")],
        ["Status", upload.get("status")],
        ["Network performed", upload.get("network_performed")],
        ["Target", upload.get("target")],
        ["Plan file", upload.get("plan_file")],
    ]
    lines.extend(_table(["Field", "Value"], [row for row in rows if row[1] is not None]))
    boundary = upload.get("boundary")
    if boundary:
        lines.extend(["", str(boundary), ""])


def render_markdown(
    manifest: dict[str, Any],
    *,
    problems: Optional[list[str]] = None,
    tail_lines: int = 40,
    generated_utc: Optional[str] = None,
) -> str:
    summary = summarize(manifest)
    bundle = manifest.get("bundle") or {}
    tool = manifest.get("tool") or {}
    system = manifest.get("system") or {}
    os_info = system.get("os") or {}
    lines: list[str] = [
        "# Blender crash-monitor report — {}".format(bundle.get("id") or "bundle"),
        "",
        "Generated {} by {} {} (prototype) · schema `{}` · report `{}`".format(
            generated_utc or utc_iso(),
            tool.get("name", "crashmon"),
            tool.get("version", __version__),
            manifest.get("schema_version", SCHEMA_VERSION),
            REPORT_SCHEMA,
        ),
        "",
        "## Verdict",
        "",
        "**{}**".format(summary["label"]),
        "",
        "Bundle kind `{}`, created `{}` on {} {}.".format(
            bundle.get("kind"),
            bundle.get("created_utc"),
            os_info.get("name") or "unknown OS",
            os_info.get("release") or os_info.get("version") or "",
        ).rstrip(),
        "",
    ]
    rows = [
        ["Crash suspected", "yes" if summary["crash_suspected"] else "no"],
        ["Exit category", summary["exit_category"] or "n/a"],
        ["Logs", "{} (raw retained: {})".format(summary["log_count"], "yes" if summary["retained_logs"] else "no/tail only")],
        ["Crash artifacts", "{} found, {} copied".format(summary["artifact_count"], summary["copied_artifact_count"])],
        ["Upload", "requested, dry-run only" if summary["upload_requested"] else "disabled"],
    ]
    lines.extend(_table(["Summary", "Value"], rows))
    lines.append("")
    if problems:
        lines.extend(["## Validation problems", ""])
        lines.extend(["- {}".format(problem) for problem in problems])
        lines.append("")
    else:
        lines.extend(["Manifest validation: no schema problems found.", ""])
    _run_section(manifest.get("run"), lines)
    _exit_section(summary, lines)
    _artifacts_section(manifest.get("crash_artifacts") or [], lines)
    _logs_section(manifest.get("logs") or [], lines, tail_lines)
    _system_section(manifest, lines)
    _platform_section(manifest, lines)
    _environment_section(manifest, lines)
    _privacy_section(manifest, summary, lines)
    _upload_section(manifest, lines)
    limitations = manifest.get("limitations") or []
    if limitations:
        lines.extend(["## Limitations", ""])
        lines.extend(["- {}".format(item) for item in limitations])
        lines.append("")
    notes = manifest.get("notes") or []
    if notes:
        lines.extend(["## Notes", ""])
        lines.extend(["- {}".format(item) for item in notes])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json_report(
    manifest: dict[str, Any],
    *,
    problems: Optional[list[str]] = None,
    generated_utc: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "report_schema": REPORT_SCHEMA,
        "generated_utc": generated_utc or utc_iso(),
        "summary": summarize(manifest),
        "validation_problems": list(problems or []),
        "manifest": manifest,
    }


def _json_pretty(value: Any) -> str:
    import json

    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def load_for_report(bundle_dir: os.PathLike[str] | str, *, strict: bool = False) -> tuple[dict[str, Any], list[str]]:
    return load_manifest(bundle_dir, strict=strict)


def write_report(
    bundle_dir: os.PathLike[str] | str,
    *,
    fmt: str = "md",
    output: Optional[os.PathLike[str] | str] = None,
    tail_lines: int = 40,
    strict: bool = False,
) -> Path:
    manifest, problems = load_for_report(bundle_dir, strict=strict)
    if fmt not in ("md", "markdown", "json"):
        raise ValueError("unsupported report format: {!r}".format(fmt))
    if fmt in ("md", "markdown"):
        content = render_markdown(manifest, problems=problems, tail_lines=tail_lines)
        suffix = ".md"
    else:
        import json

        content = json.dumps(
            render_json_report(manifest, problems=problems), indent=2, ensure_ascii=False
        ) + "\n"
        suffix = ".json"
    destination = Path(output) if output else Path(bundle_dir) / ("report" + suffix)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination
