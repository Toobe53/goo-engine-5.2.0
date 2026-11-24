#!/usr/bin/env python3
"""Probe the local prerequisites for a GOO Engine (Blender 5.2 LTS) source build.

This script does NOT configure or build anything. It only inspects the host and
prints a JSON (default) or human-readable report, so a developer or CI job can
fail fast before downloading precompiled libraries or starting a long compile.

Checks marked required cause a non-zero exit when they fail:

  * CMake >= 3.21, Ninja, Git, Git LFS, GNU make (Unix)
  * a C and a C++ compiler meeting Blender's minimums:
      - GCC >= 14.0.0, Clang >= 17.0.0 (Linux/macOS)
      - MSVC 19.44.35216 (Visual Studio 2022 17.14.14) on Windows
  * free disk space >= --disk-gb (default 25 GiB) on the repository filesystem
  * expected repository layout (CMakeLists.txt, GNUmakefile, make.bat, ...)

Warnings do not fail the probe:

  * precompiled libraries under lib/<platform>_<arch> missing or empty (a fresh
    checkout is expected to run ``make update`` first); use --require-libs to
    make this fatal
  * Python version, missing platform python executable, unsupported CPU arch

Usage:
    python tools/ci/build_probe.py [--text] [--repo-root PATH] [--disk-gb 25]
                                   [--skip-libs] [--require-libs] [--output FILE]

Set --repo-root when running from outside the source checkout.

Exit codes:
    0  all required checks passed
    1  at least one required check failed
    2  the repository root is missing or not a GOO Engine/Blender checkout

The probe deliberately does not execute the full build. Its answer says
"prerequisites look usable", not "the build will succeed".
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = "goo-engine/build-probe/1"
CMAKE_MIN = (3, 21, 0)
GCC_MIN = (14, 0, 0)
CLANG_MIN = (17, 0, 0)
MSVC_MIN = (19, 44, 35216)
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_command(
    argv: List[str], timeout: int = 30, env: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        return {
            "argv": argv,
            "returncode": proc.returncode,
            "output": (proc.stdout or "").strip(),
            "error": None,
        }
    except FileNotFoundError:
        return {"argv": argv, "returncode": None, "output": "", "error": f"{argv[0]} was not found"}
    except PermissionError as exc:
        return {"argv": argv, "returncode": None, "output": "", "error": f"{argv[0]} is not executable: {exc}"}
    except subprocess.TimeoutExpired:
        return {
            "argv": argv,
            "returncode": None,
            "output": "",
            "error": f"{argv[0]} timed out after {timeout}s",
        }
    except OSError as exc:
        return {"argv": argv, "returncode": None, "output": "", "error": f"{argv[0]} failed: {exc}"}


def _first_version(text: str) -> Tuple[Optional[str], Optional[Tuple[int, int, int]]]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return None, None
    parts = [int(part) for part in match.groups() if part is not None]
    while len(parts) < 3:
        parts.append(0)
    found = (parts[0], parts[1], parts[2])
    return ".".join(str(part) for part in found), found


def _at_least(found: Optional[Tuple[int, int, int]], minimum: Tuple[int, int, int]) -> bool:
    return found is not None and found >= minimum


def _format_minimum(minimum: Tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in minimum)


def _dedupe(values: List[Optional[str]]) -> List[str]:
    result: List[str] = []
    for value in values:
        if not value:
            continue
        value = value.strip()
        if value and value not in result:
            result.append(value)
    return result


def _compiler_command_name(value: str) -> str:
    """Resolve CC/CXX values such as ``ccache gcc-14`` to the real binary."""
    try:
        words = shlex.split(value)
    except ValueError:
        words = value.split()
    if not words:
        return value
    if Path(words[0]).name in ("ccache", "sccache") and len(words) > 1:
        return words[1]
    return words[0]


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


class ProbeReport:
    def __init__(self, repo_root: Path, disk_gb: float) -> None:
        self.repo_root = repo_root
        self.disk_gb = disk_gb
        self.checks: List[Dict[str, Any]] = []
        self.tools: Dict[str, Any] = {}
        self.extra: Dict[str, Any] = {}

    def add(self, check_id: str, required: bool, ok: bool, detail: str, **extra: Any) -> None:
        entry: Dict[str, Any] = {
            "id": check_id,
            "required": required,
            "ok": ok,
            "detail": detail,
        }
        entry.update(extra)
        self.checks.append(entry)

    def to_dict(self) -> Dict[str, Any]:
        required = [check for check in self.checks if check["required"]]
        failed = [check for check in required if not check["ok"]]
        warnings = [check for check in self.checks if not check["required"] and not check["ok"]]
        extra = dict(self.extra)
        extra_repo = extra.pop("repo", {})
        report = {
            "schema": SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "host": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python_version": platform.python_version(),
                "python_executable": sys.executable,
            },
            "repo": {
                "root": str(self.repo_root),
                "is_git_checkout": (self.repo_root / ".git").exists(),
                **extra_repo,
            },
            "tools": self.tools,
            "checks": self.checks,
            "summary": {
                "ok": not failed,
                "required_checks": len(required),
                "passed": len(required) - len(failed),
                "failed": len(failed),
                "warnings": len(warnings),
            },
        }
        report.update(extra)
        return report


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def probe_cmake(report: ProbeReport) -> None:
    result = _run_command(["cmake", "--version"])
    version, parsed = _first_version(result["output"]) if result["returncode"] == 0 else (None, None)
    path = shutil.which("cmake")
    ok = _at_least(parsed, CMAKE_MIN)
    detail = (
        f"cmake {version} at {path}"
        if ok
        else f"cmake >= {_format_minimum(CMAKE_MIN)} is required; "
        f"found: {version or result['error'] or result['output'] or 'not available'}"
    )
    report.tools["cmake"] = {
        "path": path,
        "version": version,
        "minimum": _format_minimum(CMAKE_MIN),
        "ok": ok,
    }
    report.add("cmake", True, ok, detail)


def probe_ninja(report: ProbeReport) -> None:
    result = _run_command(["ninja", "--version"])
    version, _parsed = _first_version(result["output"])
    path = shutil.which("ninja")
    ok = result["returncode"] == 0 and version is not None
    detail = (
        f"ninja {version} at {path}"
        if ok
        else f"ninja is required (GNUmakefile `ninja` target and make.bat); problem: "
        f"{result['error'] or result['output'] or 'not available'}"
    )
    report.tools["ninja"] = {"path": path, "version": version, "ok": ok}
    report.add("ninja", True, ok, detail)


def probe_git(report: ProbeReport) -> None:
    git_result = _run_command(["git", "--version"])
    git_version, _parsed = _first_version(git_result["output"])
    git_path = shutil.which("git")
    git_ok = git_result["returncode"] == 0 and git_path is not None
    report.tools["git"] = {"path": git_path, "version": git_version, "ok": git_ok}
    report.add(
        "git",
        True,
        git_ok,
        f"git {git_version} at {git_path}"
        if git_ok
        else f"git is required by make_update.py; problem: {git_result['error'] or git_result['output']}",
    )

    lfs_result = _run_command(["git", "lfs", "version"])
    lfs_version, _parsed = _first_version(lfs_result["output"])
    lfs_ok = lfs_result["returncode"] == 0 and lfs_version is not None
    report.tools["git_lfs"] = {
        "path": git_path,
        "version": lfs_version,
        "ok": lfs_ok,
    }
    report.add(
        "git-lfs",
        True,
        lfs_ok,
        f"git-lfs {lfs_version}"
        if lfs_ok
        else "git-lfs is required to fetch the precompiled libraries "
        f"(install git-lfs and run `git lfs install`); problem: "
        f"{lfs_result['error'] or lfs_result['output']}",
    )


def probe_make(report: ProbeReport) -> None:
    if platform.system() == "Windows":
        report.tools["make"] = {"ok": None, "detail": "not used on Windows (make.bat is used instead)"}
        return
    result = _run_command(["make", "--version"])
    version, _parsed = _first_version(result["output"])
    path = shutil.which("make")
    ok = result["returncode"] == 0 and path is not None
    report.tools["make"] = {"path": path, "version": version, "ok": ok}
    report.add(
        "make",
        True,
        ok,
        f"make {version} at {path}"
        if ok
        else f"GNU make is required for the Unix `make release ninja` flow; problem: "
        f"{result['error'] or result['output']}",
    )


def probe_python(report: ProbeReport) -> None:
    version = platform.python_version()
    interpreter = sys.executable
    ok = sys.version_info >= (3, 8)
    detail = (
        f"python {version} at {interpreter}"
        if ok
        else f"python 3.8+ is recommended to run build scripts; found {version}"
    )
    report.tools["python"] = {"path": interpreter, "version": version, "ok": ok}
    report.add("python", False, ok, detail)


def probe_disk(report: ProbeReport) -> None:
    try:
        usage = shutil.disk_usage(str(report.repo_root))
    except OSError as exc:
        report.add("disk", True, False, f"could not read disk usage for {report.repo_root}: {exc}")
        return
    free_gb = round(usage.free / (1024 ** 3), 1)
    total_gb = round(usage.total / (1024 ** 3), 1)
    ok = free_gb >= report.disk_gb
    detail = (
        f"{free_gb} GiB free of {total_gb} GiB "
        f"(requires >= {report.disk_gb} GiB; build directory lives next to the repo)"
        if ok
        else f"only {free_gb} GiB free of {total_gb} GiB, need >= {report.disk_gb} GiB"
    )
    report.extra["disk"] = {
        "path": str(report.repo_root),
        "free_gb": free_gb,
        "total_gb": total_gb,
        "required_gb": report.disk_gb,
        "ok": ok,
    }
    report.add("disk", True, ok, detail)


def _expected_libdir() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if system == "Windows":
        return "windows_arm64" if arm else "windows_x64"
    if system == "Darwin":
        return "macos_arm64" if arm else "macos_x64"
    return "linux_x64"


def probe_repo(report: ProbeReport) -> None:
    root = report.repo_root
    required_paths = [
        "CMakeLists.txt",
        "GNUmakefile",
        "make.bat",
        "build_files/utils/make_update.py",
        ".gitmodules",
    ]
    missing = [relative for relative in required_paths if not (root / relative).is_file()]
    report.add(
        "repo-layout",
        True,
        not missing,
        "required build files present"
        if not missing
        else f"missing from {root}: {', '.join(missing)}",
        missing=missing,
    )

    version_text = None
    patch_text = None
    header = root / "source" / "blender" / "blenkernel" / "BKE_blender_version.h"
    if header.is_file():
        header_text = header.read_text(encoding="utf-8", errors="replace")
        version_match = re.search(r"#define\s+BLENDER_VERSION\s+(\d+)", header_text)
        patch_match = re.search(r"#define\s+BLENDER_VERSION_PATCH\s+(\d+)", header_text)
        suffix_match = re.search(r'#define\s+BLENDER_VERSION_SUFFIX\s+"([^"]*)"', header_text)
        if version_match:
            version_number = int(version_match.group(1))
            version_text = f"{version_number // 100}.{version_number % 100}"
            patch_text = patch_match.group(1) if patch_match else None
            suffix = suffix_match.group(1) if suffix_match else ""
            report.extra["repo"]["blender_version"] = version_text + (f".{patch_text}" if patch_text else "")
            if suffix:
                report.extra["repo"]["blender_version_suffix"] = suffix

    libdir = _expected_libdir()
    report.extra["repo"]["libdir"] = libdir
    arch_supported = platform.system() in ("Windows", "Linux", "Darwin")
    report.add(
        "platform",
        False,
        arch_supported,
        f"{platform.system()} {platform.machine()} / expected precompiled dir lib/{libdir}",
    )
    if platform.system() == "Linux" and platform.machine().lower() in ("aarch64", "arm64"):
        report.add(
            "platform-linux-arm64",
            False,
            False,
            "Linux arm64 is not covered by the production CI matrix; precompiled libs may be unavailable",
        )


def probe_precompiled_libraries(report: ProbeReport, required: bool) -> None:
    libdir = report.extra["repo"]["libdir"]
    lib_path = report.repo_root / "lib" / libdir
    entries: List[str] = []
    if lib_path.is_dir():
        entries = sorted(entry.name for entry in lib_path.iterdir() if not entry.name.startswith("."))
    populated = bool(entries)
    gitmodules = report.repo_root / ".gitmodules"
    declared = False
    if gitmodules.is_file():
        modules_text = gitmodules.read_text(encoding="utf-8", errors="replace")
        declared = f"path = lib/{libdir}" in modules_text
    detail = (
        f"lib/{libdir} contains {len(entries)} entries (e.g. {', '.join(entries[:4])})"
        if populated
        else f"lib/{libdir} is missing or empty; run `make update` (or the platform equivalent) "
        "to fetch precompiled libraries"
    )
    report.extra["precompiled_libraries"] = {
        "path": str(lib_path),
        "declared_in_gitmodules": declared,
        "populated": populated,
        "entry_count": len(entries),
    }
    report.add("precompiled-libraries", required, populated, detail)


# --- compilers --------------------------------------------------------------


def _candidate_compiler(name: str, minimums: Dict[str, Tuple[int, int, int]]) -> Dict[str, Any]:
    path = shutil.which(name)
    candidate: Dict[str, Any] = {
        "name": name,
        "path": path,
        "version": None,
        "family": None,
        "minimum": None,
        "ok": False,
        "detail": "",
    }
    if path is None:
        candidate["detail"] = "not found on PATH"
        return candidate
    result = _run_command([path, "--version"])
    if result["returncode"] != 0:
        result = _run_command([path, "-v"])
    output = result["output"]
    if result["returncode"] != 0 and not output:
        candidate["detail"] = result["error"] or "compiler did not report a version"
        return candidate
    lowered = (name + " " + output).lower()
    if "clang" in lowered:
        family = "clang"
    elif "gcc" in lowered or "g++" in lowered or "free software foundation" in lowered:
        family = "gcc"
    elif "microsoft" in lowered:
        family = "msvc"
    else:
        family = "unknown"
    version, parsed = _first_version(output)
    minimum = minimums.get(family)
    candidate.update({"version": version, "family": family, "minimum": _format_minimum(minimum) if minimum else None})
    if minimum is None:
        candidate["detail"] = f"{name} {version or '?'}: unsupported compiler family ({family})"
        return candidate
    if _at_least(parsed, minimum):
        candidate["ok"] = True
        candidate["detail"] = f"{name} {version} ({family}, minimum {_format_minimum(minimum)})"
    else:
        candidate["detail"] = (
            f"{name} {version or '?'} is older than the required {family} "
            f"{_format_minimum(minimum)}"
        )
    return candidate


def _compiler_group(
    report: ProbeReport, group_id: str, names: List[str], minimums: Dict[str, Tuple[int, int, int]]
) -> None:
    candidates = [_candidate_compiler(name, minimums) for name in names]
    chosen = next((candidate for candidate in candidates if candidate["ok"]), None)
    detail = (
        chosen["detail"]
        if chosen
        else "; ".join(candidate["detail"] for candidate in candidates) or "no candidates"
    )
    if chosen:
        detail = chosen["detail"]
    report.tools[group_id] = {
        "chosen": None
        if chosen is None
        else {key: chosen[key] for key in ("name", "path", "version", "family", "minimum")},
        "candidates": [{key: candidate[key] for key in ("name", "path", "version", "family", "ok")} for candidate in candidates],
    }
    report.add(group_id, True, chosen is not None, detail)


def probe_unix_compilers(report: ProbeReport) -> None:
    c_names = _dedupe(
        [
            _compiler_command_name(os.environ["CC"]) if os.environ.get("CC") else None,
            "gcc",
            "clang",
            "cc",
        ]
    )
    cxx_names = _dedupe(
        [
            _compiler_command_name(os.environ["CXX"]) if os.environ.get("CXX") else None,
            "g++",
            "clang++",
            "c++",
        ]
    )
    minimums = {"gcc": GCC_MIN, "clang": CLANG_MIN}
    _compiler_group(report, "c_compiler", c_names, minimums)
    _compiler_group(report, "cxx_compiler", cxx_names, minimums)


def _probe_msvc() -> Dict[str, Any]:
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    result: Dict[str, Any] = {"candidates": [], "chosen": None, "detail": ""}
    if not vswhere.is_file():
        result["detail"] = "vswhere.exe not found; install Visual Studio 2022 with the C++ workload"
        return result
    query = _run_command(
        [
            str(vswhere),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ]
    )
    install = (query["output"] or "").strip().splitlines()
    install_path = Path(install[0]) if install else None
    if install_path is None or not install_path.is_dir():
        result["detail"] = "vswhere did not report a Visual Studio installation with the C++ x64 toolset"
        return result
    msvc_root = install_path / "VC" / "Tools" / "MSVC"
    if not msvc_root.is_dir():
        result["detail"] = f"MSVC tools directory not found: {msvc_root}"
        return result
    version_dirs = [entry for entry in msvc_root.iterdir() if entry.is_dir()]
    version_dirs.sort(key=lambda entry: entry.name, reverse=True)
    for version_dir in version_dirs[:5]:
        cl_path = version_dir / "bin" / "Hostx64" / "x64" / "cl.exe"
        if not cl_path.is_file():
            continue
        env = os.environ.copy()
        env["GOO_PROBE_CL"] = str(cl_path)
        file_version = _run_command(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-Item -LiteralPath $env:GOO_PROBE_CL).VersionInfo.FileVersion",
            ],
            env=env,
        )
        version, parsed = _first_version(file_version["output"])
        candidate = {
            "path": str(cl_path),
            "version": version,
            "toolset": version_dir.name,
            "ok": _at_least(parsed, MSVC_MIN),
        }
        result["candidates"].append(candidate)
        if candidate["ok"] and result["chosen"] is None:
            result["chosen"] = candidate
            break
    if result["chosen"] is None:
        result["detail"] = (
            f"no cl.exe >= {_format_minimum(MSVC_MIN)} was found under {msvc_root}; "
            "install the MSVC v143 17.14.14+ toolset"
        )
    else:
        result["detail"] = (
            f"cl.exe {result['chosen']['version']} at {result['chosen']['path']} "
            f"(minimum {_format_minimum(MSVC_MIN)})"
        )
    return result


def probe_msvc(report: ProbeReport) -> None:
    if platform.system() != "Windows":
        return
    result = _probe_msvc()
    report.tools["msvc"] = {
        "chosen": result["chosen"],
        "candidates": result["candidates"],
    }
    report.add("msvc", True, result["chosen"] is not None, result["detail"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_text(report: Dict[str, Any]) -> None:
    repo = report["repo"]
    host = report["host"]
    print("GOO Engine build prerequisite probe")
    print(f"  repo: {repo['root']}")
    version = repo.get("blender_version", "unknown")
    print(f"  expected Blender version: {version} | precompiled lib dir: lib/{repo['libdir']}")
    print(f"  host: {host['system']} {host['release']} {host['machine']} | python {host['python_version']}")
    print()
    for check in report["checks"]:
        if check["ok"]:
            marker = "ok  "
        elif check["required"]:
            marker = "FAIL"
        else:
            marker = "warn"
        print(f"[{marker}] {check['id']}: {check['detail']}")
    summary = report["summary"]
    print()
    print(
        f"{summary['passed']}/{summary['required_checks']} required checks passed, "
        f"{summary['failed']} failed, {summary['warnings']} warnings"
    )
    print("result: " + ("READY (prerequisites only; this does not prove the build works)" if summary["ok"] else "NOT READY"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root",
        default=str(DEFAULT_REPO_ROOT),
        help="GOO Engine source checkout (default: two levels above this script)",
    )
    parser.add_argument("--disk-gb", type=float, default=25.0, help="required free disk space in GiB")
    parser.add_argument("--skip-libs", action="store_true", help="do not check lib/<platform>_<arch>")
    parser.add_argument(
        "--require-libs",
        action="store_true",
        help="fail if precompiled libraries are not populated (default: warning only)",
    )
    parser.add_argument("--text", action="store_true", help="human-readable output instead of JSON")
    parser.add_argument("--output", help="also write the JSON report to this file")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).expanduser().resolve()
    if not repo_root.is_dir() or not (repo_root / "CMakeLists.txt").is_file():
        print(
            f"error: {repo_root} does not look like a GOO Engine/Blender checkout "
            "(CMakeLists.txt not found)",
            file=sys.stderr,
        )
        return 2

    report = ProbeReport(repo_root, args.disk_gb)
    report.extra["repo"] = {}
    probe_repo(report)
    probe_cmake(report)
    probe_ninja(report)
    probe_git(report)
    probe_make(report)
    if platform.system() == "Windows":
        probe_msvc(report)
    else:
        probe_unix_compilers(report)
    probe_python(report)
    probe_disk(report)
    if not args.skip_libs:
        probe_precompiled_libraries(report, required=args.require_libs)

    payload = report.to_dict()
    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.text:
        _print_text(payload)
    else:
        print(json.dumps(payload, indent=2))
    return 0 if payload["summary"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
