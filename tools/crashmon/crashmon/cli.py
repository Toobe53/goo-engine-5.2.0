"""crashmon command line interface.

Subcommands:
    run      supervise a Blender process, capture logs, collect artifacts
    collect  build a bundle from existing logs/crash directories
    doctor   report platform crash-collection capabilities
    report   render a bundle manifest as Markdown or JSON
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from . import SCHEMA_VERSION, __version__
from . import collectors, probes
from . import report as report_mod
from . import upload as upload_mod
from .bundle import (
    LIMITATIONS,
    collect_environment,
    create_bundle,
    default_upload_section,
    finalize_manifest,
    load_manifest,
    make_manifest,
)
from .redaction import Redactor
from .runner import classify_returncode, run_supervised
from .util import (
    CrashmonError,
    ensure_dir,
    human_bytes,
    local_iso,
    utc_iso,
    which,
)

DEFAULT_PROBE_ARGS = [
    "--background",
    "--factory-startup",
    "--python-expr",
    "import bpy; print('crashmon startup probe:', bpy.app.version_string)",
]

ABNORMAL_EXIT_CATEGORIES = {
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


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an integer, got {!r}".format(value))
    if number <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def _nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an integer, got {!r}".format(value))
    if number < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return number


def _auto_int(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an integer (decimal or 0x hex), got {!r}".format(value))


def _signal_number(value: str) -> int:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("empty signal")
    if text.isdigit():
        return int(text)
    name = text.upper()
    if not name.startswith("SIG"):
        name = "SIG" + name
    number = getattr(signal, name, None)
    if number is None:
        raise argparse.ArgumentTypeError("unknown signal name: {}".format(value))
    return int(number)


def _strip_remainder(values: Optional[Sequence[str]]) -> list[str]:
    args = [str(value) for value in (values or [])]
    if args and args[0] == "--":
        args = args[1:]
    return args


def _redactor(args: argparse.Namespace) -> Redactor:
    return Redactor(level=getattr(args, "redact", "basic"))


def _discover_blender(explicit: Optional[str]) -> Optional[str]:
    """Find Blender without launching it. Returns None when not found."""
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)
        found = which(explicit)
        return found
    for variable in ("CRASHMON_BLENDER", "BLENDER_BINARY"):
        value = os.environ.get(variable)
        if value:
            candidate = Path(value).expanduser()
            if candidate.is_file():
                return str(candidate)
            found = which(value)
            if found:
                return found
    for executable in ("blender", "blender.exe"):
        found = which(executable)
        if found:
            return found
    if platform.system() == "Windows":
        roots = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Blender Foundation",
        ]
        matches: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            for version_dir in root.iterdir():
                candidate = version_dir / "blender.exe"
                if candidate.is_file():
                    matches.append(candidate)
        if matches:
            matches.sort(key=lambda item: item.parent.name, reverse=True)
            return str(matches[0])
    return None


def _require_blender(explicit: Optional[str]) -> str:
    found = _discover_blender(explicit)
    if not found:
        raise CrashmonError(
            "Blender executable not found. Pass --blender PATH, set CRASHMON_BLENDER, or add Blender to PATH."
        )
    return found


def _probe_context(args: argparse.Namespace, blender_path: Optional[str]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    system = probes.probe_system()
    gpu = {"skipped": True, "reason": "--skip-gpu"} if getattr(args, "skip_gpu", False) else probes.probe_gpu()
    if not blender_path:
        blender: dict[str, Any] = {"path": None, "error": "no Blender executable supplied"}
    elif getattr(args, "skip_probe", False):
        blender = {"path": blender_path, "probe_skipped": True}
    else:
        blender = probes.probe_blender(blender_path)
    return system, gpu, blender


def _cutoff_epoch(since_minutes: float) -> Optional[float]:
    if not since_minutes or since_minutes <= 0:
        return None
    return time.time() - (float(since_minutes) * 60.0)


def _gather_candidates(
    args: argparse.Namespace,
    *,
    system_name: str,
    process_name: str,
    cutoff: Optional[float],
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    candidates: list[dict[str, Any]] = []
    if getattr(args, "skip_artifacts", False):
        return candidates, ["Artifact collection skipped by --skip-artifacts."]
    try:
        candidates.extend(
            collectors.discover_platform_artifacts(
                system_name,
                process_name=process_name,
                cutoff_epoch=cutoff,
                crashdumps_dir=getattr(args, "crashdumps_dir", None),
                errors=errors,
            )
        )
    except Exception as exc:  # defensive: collection must not abort a bundle
        errors.append("platform artifact discovery failed: {}: {}".format(type(exc).__name__, exc))
    for directory in getattr(args, "crash_dir", []) or []:
        try:
            candidates.extend(
                collectors.discover_generic_crash_dirs([directory], process_name, cutoff, errors)
            )
        except Exception as exc:
            errors.append("crash directory {} failed: {}: {}".format(directory, type(exc).__name__, exc))
    return candidates, errors


def _materialize(
    args: argparse.Namespace,
    bundle: Path,
    redactor: Redactor,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    return collectors.materialize_candidates(
        candidates,
        bundle,
        redactor,
        max_artifact_bytes=getattr(args, "max_artifact_bytes", 64 * 1024 * 1024),
        max_total_artifact_bytes=getattr(args, "max_total_artifact_bytes", 256 * 1024 * 1024),
        max_artifacts=getattr(args, "max_artifacts", 20),
    )


def _extract_system_core(
    args: argparse.Namespace,
    bundle: Path,
    redactor: Redactor,
    *,
    process_name: str,
    system_name: str,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if not getattr(args, "include_system_cores", False):
        return None, None
    if system_name != "Linux":
        return None, "--include-system-cores is only meaningful on Linux; skipped on {}.".format(system_name)
    destination = bundle / "crash" / "system-core-{}.dump".format(process_name)
    result = collectors.copy_latest_linux_core(process_name, destination)
    if not result.get("ok"):
        return None, "system core extraction failed: {}".format(result.get("error"))
    entry = collectors.register_bundle_file(
        destination,
        bundle,
        redactor,
        source="linux-coredumpctl",
        kind="core-dump",
        note="Core extracted by explicit --include-system-cores opt-in via coredumpctl.",
    )
    return entry, "system core extracted into the bundle; binary contents are not redactable."


def _upload_section(args: argparse.Namespace, bundle: Path) -> dict[str, Any]:
    url = getattr(args, "upload_url", None)
    if not url:
        return default_upload_section()
    return upload_mod.build_upload_plan(
        bundle,
        upload_url=url,
        consent=bool(getattr(args, "consent", False)),
    )


def _manifest_and_report(
    args: argparse.Namespace,
    bundle: Path,
    redactor: Redactor,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], list[str], Optional[Path]]:
    finalized = finalize_manifest(bundle, manifest, redactor)
    _, problems = load_manifest(bundle)
    report_path: Optional[Path] = None
    if getattr(args, "report", False):
        report_path = report_mod.write_report(bundle, fmt="md", tail_lines=40)
    return finalized, problems, report_path


def _run_summary(bundle: Path, manifest: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    summary = report_mod.summarize(manifest)
    return {
        "bundle_dir": str(bundle),
        "manifest_path": str(bundle / "manifest.json"),
        "report_path": str(bundle / "report.md") if (bundle / "report.md").is_file() else None,
        "summary": summary,
        "validation_problems": problems,
    }


def _print_run_summary(payload: dict[str, Any], manifest: dict[str, Any], *, report_path: Optional[Path]) -> None:
    summary = payload["summary"]
    run = manifest.get("run") or {}
    exit_info = run.get("exit") or {}
    print("")
    print("crashmon bundle: {}".format(payload["bundle_dir"]))
    print("  verdict: {}".format(summary["label"]))
    print(
        "  exit: {} ({})".format(
            exit_info.get("description") or "collected, no exit status", exit_info.get("category") or "n/a"
        )
    )
    print(
        "  logs: {}  artifacts: {} found / {} copied".format(
            summary["log_count"], summary["artifact_count"], summary["copied_artifact_count"]
        )
    )
    print(
        "  privacy: redact={} raw_logs={} network={}".format(
            (manifest.get("privacy") or {}).get("level"),
            (manifest.get("privacy") or {}).get("raw_logs_included"),
            (manifest.get("privacy") or {}).get("network"),
        )
    )
    upload = manifest.get("upload") or {}
    if upload.get("requested"):
        print("  upload: dry-run plan only ({})".format(upload.get("plan_file")))
    if report_path:
        print("  report: {}".format(report_path))
    if payload["validation_problems"]:
        print("  validation problems: {}".format("; ".join(payload["validation_problems"])), file=sys.stderr)


def _exit_code_for(manifest: dict[str, Any], *, fail_on_crash: bool, spawn_error: bool) -> int:
    if spawn_error:
        return 1
    if not fail_on_crash:
        return 0
    run = manifest.get("run") or {}
    category = (run.get("exit") or {}).get("category")
    if category in ("crash", "exception"):
        return 10
    if category == "timeout":
        return 11
    if category in ABNORMAL_EXIT_CATEGORIES:
        return 12
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    upload_mod.validate_upload_arguments(args.upload_url, args.consent)
    blender_path = _require_blender(args.blender)
    redactor = _redactor(args)
    bundle = create_bundle(args.output_dir, "run", args.name)
    notes: list[str] = []

    system, gpu, blender = _probe_context(args, blender_path)
    blender_args = _strip_remainder(args.blender_args)
    used_default = not blender_args
    if used_default:
        blender_args = list(DEFAULT_PROBE_ARGS)
        notes.append(
            "No Blender arguments were supplied after --; crashmon ran a short --background startup probe "
            "instead of an interactive session."
        )
    argv = [blender_path] + blender_args
    notes.append("Supervisor observes only the direct Blender child process.")
    if getattr(args, "no_raw_logs", False):
        notes.append("Raw stdout/stderr logs were not retained; only manifest tails were kept.")

    run_result = run_supervised(
        argv,
        bundle,
        redactor=redactor,
        retain_logs=not getattr(args, "no_raw_logs", False),
        max_log_bytes=args.max_log_bytes,
        log_backups=args.log_backups,
        tail_lines=args.tail_lines,
        timeout=args.timeout,
        echo=not (args.json or getattr(args, "quiet", False)),
    )

    append_notes = notes

    process_name = Path(blender_path).stem or "blender"
    cutoff = _cutoff_epoch(args.since_minutes)
    candidates, collection_errors = _gather_candidates(
        args, system_name=system["os"].get("name", platform.system()), process_name=process_name, cutoff=cutoff
    )
    artifacts = _materialize(args, bundle, redactor, candidates)
    core_entry, core_note = _extract_system_core(
        args, bundle, redactor, process_name=process_name, system_name=system["os"].get("name", platform.system())
    )
    if core_entry:
        artifacts.append(core_entry)
    if core_note:
        append_notes.append(core_note)
    append_notes.extend(collection_errors)

    upload_section = _upload_section(args, bundle)
    if upload_section.get("warnings"):
        append_notes.extend(str(item) for item in upload_section["warnings"])

    manifest = make_manifest(
        kind="run",
        bundle_dir=bundle,
        redactor=redactor,
        raw_logs_included=not getattr(args, "no_raw_logs", False),
        environment=collect_environment(),
        system=system,
        gpu=gpu,
        blender=blender,
        platform_info=collectors.platform_platform_info(
            process_name=process_name, cutoff_epoch=cutoff
        ),
        run=run_result,
        logs=run_result.get("logs") or [],
        crash_artifacts=artifacts,
        upload=upload_section,
        notes=append_notes,
    )
    finalized, problems, report_path = _manifest_and_report(args, bundle, redactor, manifest)
    payload = _run_summary(bundle, finalized, problems)

    if args.json:
        _print_json(payload)
    else:
        _print_run_summary(payload, finalized, report_path=report_path)
    spawn_error = bool(run_result.get("spawn_error"))
    return _exit_code_for(finalized, fail_on_crash=args.fail_on_crash, spawn_error=spawn_error)


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------


def _synthetic_run(args: argparse.Namespace, logs: list[dict[str, Any]], notes: list[str]) -> Optional[dict[str, Any]]:
    exit_info: Optional[dict[str, Any]] = None
    description = None
    if args.exit_code_hex is not None:
        try:
            value = int(args.exit_code_hex, 16)
        except ValueError:
            raise CrashmonError("--exit-code-hex must be hexadecimal, e.g. 0xC0000005")
        exit_info = classify_returncode(value, system="Windows")
        description = "--exit-code-hex"
    elif args.exit_code is not None:
        value = int(args.exit_code)
        system = "Windows" if value >= 0x80000000 or value < 0 else platform.system()
        exit_info = classify_returncode(value, system=system)
        description = "--exit-code"
    elif args.signal_number is not None:
        exit_info = classify_returncode(-int(args.signal_number), system="Linux")
        description = "--signal"
    if exit_info is None:
        return None
    notes.append("Exit status supplied manually via {}; it was not observed by the supervisor.".format(description))
    return {
        "started_utc": None,
        "ended_utc": utc_iso(),
        "duration_s": None,
        "timeout_seconds": None,
        "timed_out": False,
        "command": ["<collected>"],
        "cwd": None,
        "exit": exit_info,
        "logs": logs,
        "spawn_error": None,
        "source": "collect",
    }


def cmd_collect(args: argparse.Namespace) -> int:
    upload_mod.validate_upload_arguments(args.upload_url, args.consent)
    redactor = _redactor(args)
    bundle = create_bundle(args.output_dir, "collect", args.name)
    notes: list[str] = []
    system, gpu, blender = _probe_context(
        args, _discover_blender(args.blender) if args.blender else None
    )
    system_name = system["os"].get("name", platform.system())
    process_name = collectors.normalize_process_name(args.process_name)
    cutoff = _cutoff_epoch(args.since_minutes)

    logs: list[dict[str, Any]] = []
    for log_path in args.log:
        try:
            entry = collectors.import_log_file(log_path, bundle, redactor, tail_lines=args.tail_lines)
            logs.append(entry)
            notes.append("Imported log {} ({}).".format(log_path, entry.get("note", "")))
        except (CrashmonError, OSError) as exc:
            notes.append("Could not import log {}: {}".format(log_path, exc))

    candidates, collection_errors = _gather_candidates(
        args, system_name=system_name, process_name=process_name, cutoff=cutoff
    )
    artifacts = _materialize(args, bundle, redactor, candidates)
    core_entry, core_note = _extract_system_core(
        args, bundle, redactor, process_name=process_name, system_name=system_name
    )
    if core_entry:
        artifacts.append(core_entry)
    if core_note:
        notes.append(core_note)
    notes.extend(collection_errors)

    run = _synthetic_run(args, logs, notes)
    upload_section = _upload_section(args, bundle)
    if upload_section.get("warnings"):
        notes.extend(str(item) for item in upload_section["warnings"])
    manifest = make_manifest(
        kind="collect",
        bundle_dir=bundle,
        redactor=redactor,
        raw_logs_included=bool(logs),
        environment=collect_environment(),
        system=system,
        gpu=gpu,
        blender=blender,
        platform_info=collectors.platform_platform_info(process_name=process_name, cutoff_epoch=cutoff),
        run=run,
        logs=logs,
        crash_artifacts=artifacts,
        upload=upload_section,
        notes=notes,
    )
    finalized, problems, report_path = _manifest_and_report(args, bundle, redactor, manifest)
    payload = _run_summary(bundle, finalized, problems)
    if args.json:
        _print_json(payload)
    else:
        _print_run_summary(payload, finalized, report_path=report_path)
    return _exit_code_for(finalized, fail_on_crash=args.fail_on_crash, spawn_error=False)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _doctor_payload(args: argparse.Namespace) -> dict[str, Any]:
    system = probes.probe_system()
    gpu = {"skipped": True, "reason": "--skip-gpu"} if args.skip_gpu else probes.probe_gpu()
    blender_path = _discover_blender(args.blender)
    blender = (
        probes.probe_blender(blender_path, timeout=args.timeout)
        if blender_path
        else {"path": None, "exists": False, "error": "Blender executable not found (pass --blender PATH)"}
    )
    platform_info = collectors.platform_platform_info()
    system_name = platform.system()

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: Optional[bool], detail: str) -> None:
        status = "ok" if ok is True else "warn" if ok is False else "info"
        checks.append({"name": name, "status": status, "ok": ok, "detail": detail})

    py_version = tuple(int(part) for part in platform.python_version_tuple()[:2])
    check("python", py_version >= (3, 9), "Python {} (requires 3.9+)".format(platform.python_version()))
    check("platform", system_name in ("Windows", "Linux", "Darwin"), "platform is {}".format(system_name))
    check(
        "blender",
        bool(blender.get("exists")),
        "{} ({})".format(blender.get("path") or "not found", blender.get("version") or blender.get("error") or "not probed"),
    )

    platform_section = platform_info.get(system_name.lower()) or {}
    available = bool(platform_section.get("available"))
    check(
        "crash-artifact-source",
        available,
        "platform collector reports available={} for {}".format(available, system_name),
    )
    check("upload", None, "network transport intentionally not implemented; uploads are dry-run only")
    check("environment", True, "only allowlisted variables are collected")

    warnings: list[str] = []
    for source_name, value in (
        ("system", system.get("errors")),
        ("gpu", gpu.get("errors")),
        ("platform", platform_section.get("errors")),
    ):
        for item in value or []:
            warnings.append("{} probe: {}".format(source_name, item))
    if blender.get("error"):
        warnings.append("Blender probe: {}".format(blender["error"]))
    if not available:
        warnings.append(
            "No crash-artifact source is currently available. Crashes may still occur; the bundle will contain "
            "logs and exit status only."
        )
    if system_name == "Windows":
        localdumps = (platform_section.get("localdumps_registry") or {})
        if not localdumps.get("enabled_hint"):
            warnings.append(
                "Windows LocalDumps does not appear to be configured; local minidumps may be absent. "
                "crashmon will not change this setting."
            )
    elif system_name == "Linux":
        coredumpctl = (platform_section.get("coredumpctl") or {})
        if not coredumpctl.get("available"):
            warnings.append("coredumpctl not found; system core extraction is unavailable.")
        core_pattern = (platform_section.get("core_pattern") or {})
        if core_pattern.get("piped"):
            warnings.append(
                "core_pattern is piped; cores may be handled by a service and not visible as files. "
                "crashmon will not change core_pattern."
            )
    elif system_name == "Darwin":
        warnings.append("macOS crash reports are written asynchronously; a report may appear only after a delay.")

    return {
        "doctor_schema": "crashmon.doctor/1",
        "tool": {"name": "crashmon", "version": __version__, "prototype": True},
        "generated_utc": utc_iso(),
        "generated_local": local_iso(),
        "platform": system_name,
        "system": system,
        "gpu": gpu,
        "blender": blender,
        "platform_capabilities": platform_info,
        "checks": checks,
        "warnings": warnings,
        "network": "disabled",
        "upload": "dry-run only; requires --upload-url and --consent",
        "limitations": list(LIMITATIONS),
    }


def _doctor_available(payload: dict[str, Any]) -> bool:
    for check in payload.get("checks", []):
        if check.get("name") == "crash-artifact-source":
            return bool(check.get("ok"))
    return False


def _print_doctor(payload: dict[str, Any]) -> None:
    system = payload["system"]
    os_info = system.get("os") or {}
    print("crashmon {} doctor (prototype)".format(__version__))
    print("Platform: {} {} ({})".format(os_info.get("name"), os_info.get("release"), system.get("arch")))
    print("Python: {}".format((system.get("python") or {}).get("version_full")))
    blender = payload["blender"]
    print("Blender: {}".format(blender.get("path") or "not found"))
    if blender.get("version"):
        print("  version: {} build {}".format(blender.get("version"), blender.get("build_hash") or "unknown"))
    print("")
    print("Checks:")
    for check in payload["checks"]:
        print("  [{}] {}: {}".format(check["status"], check["name"], check["detail"]))
    print("")
    platform_section = payload["platform_capabilities"].get(payload["platform"].lower()) or {}
    if payload["platform"] == "Windows":
        crashdumps = platform_section.get("crashdumps") or {}
        print("Windows crash collection:")
        print("  CrashDumps: {} (exists={}, readable={})".format(
            crashdumps.get("path"), crashdumps.get("exists"), crashdumps.get("readable")
        ))
        print("  LocalDumps configured hint: {}".format((platform_section.get("localdumps_registry") or {}).get("enabled_hint")))
        wer_dirs = platform_section.get("wer_dirs") or []
        print("  WER directories: {} / {} exist".format(
            sum(1 for item in wer_dirs if item.get("exists")), len(wer_dirs)
        ))
        print("  WerFault.exe: {}".format((platform_section.get("werfault") or {}).get("exists")))
    elif payload["platform"] == "Linux":
        coredumpctl = platform_section.get("coredumpctl") or {}
        print("Linux crash collection:")
        print("  coredumpctl: {}".format(coredumpctl.get("path") or "not found"))
        print("  core_pattern: {}".format((platform_section.get("core_pattern") or {}).get("value")))
        print("  ulimit -c: {}".format((platform_section.get("ulimit_core") or {}).get("soft_text")))
    elif payload["platform"] == "Darwin":
        print("macOS DiagnosticReports:")
        for item in platform_section.get("diagnostic_report_dirs") or []:
            print("  {}: exists={} recent={}".format(item.get("path"), item.get("exists"), item.get("recent_report_files")))
    print("")
    if payload["warnings"]:
        print("Warnings:")
        for warning in payload["warnings"]:
            print("  - {}".format(warning))
        print("")
    print("Upload: disabled by default; dry-run only.")
    print("Result: {}".format("READY" if _doctor_available(payload) else "LIMITED (logs only)"))


def cmd_doctor(args: argparse.Namespace) -> int:
    payload = _doctor_payload(args)
    if args.json:
        _print_json(payload)
    else:
        _print_doctor(payload)
    if args.strict and not _doctor_available(payload):
        return 2
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle).expanduser()
    manifest, problems = load_manifest(bundle, strict=False)
    if args.strict and problems:
        raise CrashmonError("manifest failed validation: {}".format("; ".join(problems)))
    if args.format == "json":
        content = json.dumps(
            report_mod.render_json_report(manifest, problems=problems), indent=2, ensure_ascii=False
        ) + "\n"
        default_suffix = ".json"
    else:
        content = report_mod.render_markdown(manifest, problems=problems, tail_lines=args.tail_lines)
        default_suffix = ".md"
    if args.output:
        destination = Path(args.output).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        print(str(destination))
    else:
        sys.stdout.write(content)
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--blender", help="Blender executable (default: auto-detect)")
    parser.add_argument("--output-dir", default="crashmon-bundles", help="directory that receives bundles")
    parser.add_argument("--name", help="bundle directory name override")
    parser.add_argument("--redact", choices=Redactor.LEVELS, default="basic", help="redaction level (default: basic)")
    parser.add_argument("--skip-gpu", action="store_true", help="skip GPU probing")
    parser.add_argument("--skip-probe", action="store_true", help="skip `blender --version` probing")
    parser.add_argument("--skip-artifacts", action="store_true", help="do not search for crash artifacts")
    parser.add_argument("--crash-dir", action="append", default=[], metavar="DIR", help="extra crash directory/file (repeatable)")
    parser.add_argument("--crashdumps-dir", help="override %%LOCALAPPDATA%%\\CrashDumps (testing/portable use)")
    parser.add_argument("--include-system-cores", action="store_true", help="Linux: opt in to coredumpctl extraction")
    parser.add_argument("--max-log-bytes", type=_positive_int, default=64 * 1024 * 1024)
    parser.add_argument("--log-backups", type=_nonnegative_int, default=3)
    parser.add_argument("--max-artifact-bytes", type=_positive_int, default=64 * 1024 * 1024)
    parser.add_argument("--max-total-artifact-bytes", type=_positive_int, default=256 * 1024 * 1024)
    parser.add_argument("--max-artifacts", type=_positive_int, default=20)
    parser.add_argument("--upload-url", help="explicit opt-in dry-run upload target; requires --consent")
    parser.add_argument("--consent", action="store_true", help="acknowledge dry-run upload planning (no network)")
    parser.add_argument("--json", action="store_true", help="machine-readable summary; suppresses child echo")
    parser.add_argument("--report", action="store_true", help="also write report.md into the bundle")
    parser.add_argument("--fail-on-crash", action="store_true", help="exit 10/11/12 for crash/timeout/abnormal")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crashmon",
        description="Prototype cross-platform Blender crash monitor (standard library only, no network).",
    )
    parser.add_argument("--version", action="version", version="%(prog)s {} (prototype)".format(__version__))
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run_parser = subparsers.add_parser(
        "run",
        help="supervise Blender, capture logs, collect crash artifacts",
        description=(
            "Run Blender as a direct child. Without arguments after --, a short background startup probe is used. "
            "Crash artifacts are discovered after the process exits."
        ),
    )
    _add_common_options(run_parser)
    run_parser.add_argument("--timeout", type=float, default=0.0, help="seconds before terminate/kill (0 = none)")
    run_parser.add_argument("--since-minutes", type=float, default=30.0, help="artifact age window in minutes (0 = any)")
    run_parser.add_argument("--tail-lines", type=_nonnegative_int, default=80, help="log tail lines stored in the manifest")
    run_parser.add_argument("--no-raw-logs", action="store_true", help="keep only redacted tails, not full logs")
    run_parser.add_argument("--quiet", action="store_true", help="do not echo child output")
    run_parser.add_argument(
        "blender_args",
        nargs=argparse.REMAINDER,
        metavar="-- BLENDER_ARGS",
        help="arguments passed to Blender (use -- before them)",
    )
    run_parser.set_defaults(handler=cmd_run)

    collect_parser = subparsers.add_parser(
        "collect",
        help="build a bundle from existing logs and crash directories",
        description="Collect existing logs/artifacts without launching Blender.",
    )
    _add_common_options(collect_parser)
    collect_parser.add_argument("--log", action="append", default=[], metavar="FILE", help="log file to import (repeatable)")
    collect_parser.add_argument("--process-name", default="blender", help="process name used to match artifacts")
    collect_parser.add_argument("--since-minutes", type=float, default=1440.0, help="artifact age window in minutes (0 = any)")
    collect_parser.add_argument("--tail-lines", type=_nonnegative_int, default=80)
    exit_group = collect_parser.add_mutually_exclusive_group()
    exit_group.add_argument("--exit-code", type=_auto_int, help="observed exit code (decimal or 0x status)")
    exit_group.add_argument("--exit-code-hex", help="observed Windows NTSTATUS in hex, e.g. 0xC0000005")
    exit_group.add_argument("--signal", dest="signal_number", type=_signal_number, help="observed POSIX signal name/number")
    collect_parser.set_defaults(handler=cmd_collect)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="report platform crash-collection capabilities",
        description="Read-only capability report. Exits 0 even when capabilities are missing (unless --strict).",
    )
    doctor_parser.add_argument("--blender", help="Blender executable to probe")
    doctor_parser.add_argument("--skip-gpu", action="store_true")
    doctor_parser.add_argument("--timeout", type=float, default=20.0, help="Blender probe timeout in seconds")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument("--strict", action="store_true", help="exit 2 when no artifact source is available")
    doctor_parser.set_defaults(handler=cmd_doctor)

    report_parser = subparsers.add_parser(
        "report",
        help="render a bundle manifest as Markdown or JSON",
        description="Read manifest.json and render a report; never contacts the network.",
    )
    report_parser.add_argument("bundle", help="bundle directory or manifest.json path")
    report_parser.add_argument("--format", choices=("md", "json"), default="md")
    report_parser.add_argument("-o", "--output", help="write to this file instead of stdout")
    report_parser.add_argument("--tail-lines", type=_nonnegative_int, default=40)
    report_parser.add_argument("--strict", action="store_true", help="fail when the manifest has schema problems")
    report_parser.set_defaults(handler=cmd_report)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return int(handler(args))
    except CrashmonError as exc:
        print("crashmon: error: {}".format(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print("crashmon: error: {}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("crashmon: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
