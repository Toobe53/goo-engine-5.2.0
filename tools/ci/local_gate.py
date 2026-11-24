#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Goo Engine Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run the local release gate for the Windows/Linux production branch.

This is the local counterpart of the hosted build workflow.  It is deliberately
read-only: every stage either inspects files or runs tests, and the release
bundle must already have been produced by ``build_release.py``.

Exit codes:
    0  every selected stage passed
    1  one or more stages failed
    2  usage error / repository root not found
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "CMakeLists.txt").is_file() and (candidate / "tools").is_dir():
            return candidate
    raise SystemExit("local_gate: repository root not found (run inside the checkout)")


class Stage:
    def __init__(
        self,
        name: str,
        command: list[str],
        *,
        optional: bool = False,
        cwd: Path | None = None,
        skip_reason: str | None = None,
    ) -> None:
        self.name = name
        self.command = command
        self.optional = optional
        self.cwd = cwd
        self.skip_reason = skip_reason


def python(*args: str) -> list[str]:
    return [sys.executable, *args]


def build_stages(repo: Path, args: argparse.Namespace) -> list[Stage]:
    py = sys.executable
    stages = [
        Stage("python-compile", python("-m", "compileall", "-q", "tools", "tests/python")),
        Stage(
            "crashmon-tests",
            python("-m", "unittest", "discover", "-s", "tests", "-v"),
            cwd=repo / "tools" / "crashmon",
        ),
        Stage("update-tests", python("tools/release/test_update.py")),
        Stage("workflow-validation", python("tools/ci/validate_workflow.py", "--quiet")),
        Stage("driver-matrix-validation", python("tools/driver_matrix/run_matrix.py", "--validate-only")),
    ]

    release_dir = repo / args.release_dir
    if release_dir.is_dir():
        stages.append(
            Stage(
                "release-verify",
                python("tools/release/verify_release.py", "--release-dir", str(release_dir)),
            )
        )
    else:
        stages.append(
            Stage(
                "release-verify",
                [],
                optional=True,
                skip_reason="release directory not found: %s" % release_dir,
            )
        )

    if args.with_ctest:
        ctest = args.ctest or shutil.which("ctest")
        if not ctest:
            stages.append(
                Stage(
                    "goo-ctest",
                    [],
                    optional=True,
                    skip_reason="ctest not found; pass --ctest PATH",
                )
            )
        else:
            stages.append(
                Stage(
                    "goo-ctest",
                    [ctest, "--test-dir", args.ctest_dir, "-L", "goo", "--output-on-failure", "-j", "1"],
                )
            )

    git_exe = shutil.which("git")
    if not git_exe and os.name == "nt":
        candidate = Path(r"C:\Program Files\Git\cmd\git.exe")
        if candidate.is_file():
            git_exe = str(candidate)
    if git_exe:
        stages.append(Stage("git-diff-check", [git_exe, "diff", "--check"]))

    return stages


def run_stage(repo: Path, stage: Stage, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "name": stage.name,
        "command": stage.command,
        "status": "pass",
        "returncode": 0,
        "duration_s": 0.0,
        "output_tail": "",
    }
    skipped = getattr(stage, "skip_reason", None)
    if skipped:
        result.update(status="skip", returncode=None, reason=skipped)
        return result
    try:
        proc = subprocess.run(
            stage.command,
            cwd=str(stage.cwd or repo),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result.update(status="fail", returncode=None, reason="timeout after %ss" % timeout)
        result["duration_s"] = round(time.monotonic() - started, 3)
        return result
    except OSError as exc:
        result.update(status="fail", returncode=None, reason="%s: %s" % (type(exc).__name__, exc))
        result["duration_s"] = round(time.monotonic() - started, 3)
        return result

    lines = (proc.stdout + "\n" + proc.stderr).splitlines()
    result.update(
        status="pass" if proc.returncode == 0 else "fail",
        returncode=proc.returncode,
        output_tail="\n".join(lines[-20:]),
        duration_s=round(time.monotonic() - started, 3),
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local Goo Engine production gate.")
    parser.add_argument("--release-dir", default="dist/goo-engine-5.2.1")
    parser.add_argument("--with-ctest", action="store_true", help="Also run the registered Goo CTest tests.")
    parser.add_argument("--ctest", help="Path to ctest executable (default: PATH lookup).")
    parser.add_argument("--ctest-dir", default="build-windows-lite", help="CMake build directory for CTest.")
    parser.add_argument("--timeout", type=int, default=1800, help="Per-stage timeout in seconds.")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report.")
    args = parser.parse_args(argv)

    repo = find_repo_root(Path.cwd())
    stages = build_stages(repo, args)
    results: list[dict[str, Any]] = []
    for stage in stages:
        result = run_stage(repo, stage, args.timeout)
        results.append(result)
        if not args.json:
            print("[%s] %-28s %7.2fs" % (result["status"].upper(), stage.name, result["duration_s"]))
            if result["status"] == "fail":
                print(result.get("output_tail") or result.get("reason") or "", file=sys.stderr)
        if result["status"] == "fail" and not stage.optional:
            break

    failures = [item for item in results if item["status"] == "fail"]
    report = {
        "schema": "goo-local-gate-v1",
        "repo": str(repo),
        "passed": len([item for item in results if item["status"] == "pass"]),
        "failed": len(failures),
        "skipped": len([item for item in results if item["status"] == "skip"]),
        "stages": results,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("")
        print("gate: %d passed, %d failed, %d skipped" % (report["passed"], report["failed"], report["skipped"]))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
