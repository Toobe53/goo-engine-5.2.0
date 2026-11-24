#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Goo Engine Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Build a release bundle: collect artifacts, SHA256SUMS and a manifest.

Safety model: **nothing is written unless --apply is passed**. The default is a
read-only dry-run that prints the artifact list and the manifest that would be
generated. Signing is not performed here; the signing scripts (sign_windows.ps1
and sign_macos.sh) also default to dry-run and are expected to run *before*
this script so that the recorded hashes cover the signed bytes.

Example::

    python tools/release/build_release.py --version 5.2.0 \
        --artifacts build_windows/bin --artifacts build_linux/bin --out dist/5.2.0
    python tools/release/build_release.py --version 5.2.0 \
        --artifacts build_windows/bin --out dist/5.2.0 --apply

By default the driver requirements are read from
``tools/driver_matrix/matrix.json``; pass ``--driver-matrix ""`` to omit them.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import hashlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path

SCHEMA_VERSION = "1.0"
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_MATRIX = REPO_ROOT / "tools" / "driver_matrix" / "matrix.json"
EXCLUDED_NAMES = {"SHA256SUMS", "release-manifest.json", ".DS_Store", "Thumbs.db"}
EXCLUDED_SUFFIXES = {".tmp", ".part"}


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def collect_artifacts(artifact_dirs: list, out_dir: Path) -> tuple[list, list]:
    """Return (artifacts, warnings).

    Artifacts are regular files below the given directories. Names are kept
    relative to the artifact directory and flattened into the release bundle;
    duplicate names with different content are a hard error for the caller.
    """
    artifacts = []
    warnings = []
    if not artifact_dirs:
        raise ValueError("at least one --artifacts directory is required")
    out_resolved = out_dir.resolve()
    for directory in artifact_dirs:
        root = Path(directory).expanduser()
        if not root.is_dir():
            raise ValueError("artifact directory does not exist: %s" % root)
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.name in EXCLUDED_NAMES or path.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            resolved = path.resolve()
            if resolved == out_resolved or out_resolved in resolved.parents:
                warnings.append("skipping file inside output directory: %s" % path)
                continue
            artifacts.append({
                "source": path,
                "name": path.name,
                "size": path.stat().st_size,
                "sha256": None,
            })
    return artifacts, warnings


def dedupe_artifacts(artifacts: list) -> list:
    by_name = {}
    for artifact in artifacts:
        existing = by_name.get(artifact["name"])
        if existing is None:
            by_name[artifact["name"]] = artifact
            continue
        if existing["source"].resolve() == artifact["source"].resolve():
            continue
        if existing["size"] != artifact["size"]:
            raise ValueError(
                "artifact name collision %r: %s (%d bytes) vs %s (%d bytes)"
                % (artifact["name"], existing["source"], existing["size"], artifact["source"], artifact["size"])
            )
        by_name[artifact["name"]] = existing
    return sorted(by_name.values(), key=lambda item: item["name"])


def load_driver_requirements(matrix_path: Path, warnings: list) -> list:
    if not matrix_path:
        return []
    if not matrix_path.is_file():
        warnings.append("driver matrix not found: %s (driver_requirements will be empty)" % matrix_path)
        return []
    try:
        matrix = load_json(matrix_path)
    except (OSError, json.JSONDecodeError) as ex:
        warnings.append("cannot read driver matrix %s: %s" % (matrix_path, ex))
        return []
    requirements = []
    seen = set()
    for entry in matrix.get("environments", []):
        try:
            os_info = entry["os"]
            gpu = entry["gpu"]
            min_driver = gpu["min_driver"]
            key = (
                os_info.get("family"),
                os_info.get("release"),
                os_info.get("arch"),
                gpu.get("vendor"),
                min_driver.get("scheme"),
                min_driver.get("version"),
            )
        except (KeyError, AttributeError, TypeError):
            entry_id = entry.get("id") if isinstance(entry, dict) else None
            warnings.append("skipping malformed driver matrix entry: %r" % entry_id)
            continue
        if key in seen:
            continue
        seen.add(key)
        requirements.append({
            "os": key[0],
            "os_release": key[1],
            "arch": key[2],
            "gpu_vendor": key[3],
            "scheme": key[4],
            "minimum_version": key[5],
            "display": min_driver.get("display"),
            "tier": entry.get("tier"),
            "required": bool(entry.get("required")),
            "matrix_entry": entry.get("id"),
        })
    requirements.sort(key=lambda item: (item["os"] or "", item["os_release"] or "", item["gpu_vendor"] or ""))
    return requirements


def load_dependencies(path: Path | None, declared: list, warnings: list) -> list:
    dependencies = [
        {
            "name": "python",
            "version": platform.python_version(),
            "source": "runtime",
            "required": True,
        }
    ]
    if path:
        if not path.is_file():
            warnings.append("dependencies file not found: %s" % path)
        else:
            try:
                data = load_json(path)
            except (OSError, json.JSONDecodeError) as ex:
                warnings.append("cannot read dependencies %s: %s" % (path, ex))
            else:
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("name"):
                            dependencies.append({
                                "name": str(item["name"]),
                                "version": str(item.get("version", "unknown")),
                                "source": str(item.get("source", "dependencies-file")),
                                "required": bool(item.get("required", False)),
                            })
                else:
                    warnings.append("dependencies file must contain a JSON list: %s" % path)
    for item in declared:
        if "=" not in item:
            raise ValueError("--dependency must use NAME=VERSION, got %r" % item)
        name, version = item.split("=", 1)
        if not name or not version:
            raise ValueError("--dependency must use NAME=VERSION, got %r" % item)
        dependencies.append({"name": name, "version": version, "source": "declared", "required": True})
    return dependencies


def signing_block(args) -> dict:
    windows_signed = bool(args.windows_signed)
    macos_signed = bool(args.macos_signed)
    platforms = {}
    if windows_signed:
        platforms["windows"] = {
            "tool": "signtool",
            "status": "signed",
            "identity": os.environ.get("GOO_WIN_CERT_SUBJECT") or "unknown",
        }
    else:
        platforms["windows"] = {
            "tool": "signtool",
            "status": "not-run",
            "note": "placeholder; run tools/release/sign_windows.ps1 before hashing",
        }
    if macos_signed:
        platforms["macos"] = {
            "tool": "codesign",
            "status": "signed",
            "identity": os.environ.get("GOO_MAC_SIGN_IDENTITY") or "unknown",
        }
    else:
        platforms["macos"] = {
            "tool": "codesign",
            "status": "not-run",
            "note": "placeholder; run tools/release/sign_macos.sh before hashing",
        }
    statuses = {item["status"] for item in platforms.values()}
    if statuses == {"signed"}:
        overall = "signed"
    elif "signed" in statuses:
        overall = "partial"
    else:
        overall = "unsigned-placeholder"
    return {"status": overall, "platforms": platforms}


def build_manifest(args, artifacts: list, checksums_name: str, driver_requirements: list, dependencies: list) -> tuple[dict, list]:
    warnings = []
    project = {
        "name": args.name,
        "version": args.version,
        "channel": args.channel,
    }
    commit = args.commit or os.environ.get("GITHUB_SHA") or "unknown"
    branch = args.branch or os.environ.get("GITHUB_REF_NAME") or "unknown"
    build_id = args.build_id or os.environ.get("GITHUB_RUN_ID") or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    builder = args.builder or os.environ.get("GITHUB_ACTOR") or getpass.getuser()
    if commit == "unknown":
        warnings.append("commit is unknown; pass --commit or set GITHUB_SHA")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "project": project,
        "source": {
            "commit": commit,
            "commit_short": commit[:12] if commit != "unknown" else "unknown",
            "branch": branch,
        },
        "build": {
            "id": build_id,
            "timestamp": utc_now(),
            "host": platform.node(),
            "platform": platform.platform(),
            "arch": platform.machine(),
            "python": platform.python_version(),
            "builder": builder,
        },
        "artifacts": artifacts,
        "checksums": {
            "algorithm": "sha256",
            "file": checksums_name,
            "count": len(artifacts),
        },
        "dependencies": dependencies,
        "driver_requirements": driver_requirements,
        "signing": signing_block(args),
        "limitations": [
            "A complete driver matrix requires the self-hosted GPU lab documented in docs/production/driver-matrix.md.",
            "Unsupported GPU/driver combinations are not covered by this release manifest.",
            "Signing placeholders are not proof of signature; verify with tools/release/verify_release.py --check-signatures.",
        ],
        "warnings": warnings,
    }
    return manifest, warnings


def print_plan(args, artifacts, manifest, out_dir: Path, matrix_path: Path, dry_run: bool) -> None:
    if dry_run:
        print("release build plan (dry-run; pass --apply to write files)")
    else:
        print("release build plan (writing release bundle)")
    print("  project: %s %s (%s)" % (args.name, args.version, args.channel))
    print("  out: %s" % out_dir)
    print("  artifacts: %d" % len(artifacts))
    for artifact in artifacts:
        print("    %-40s %10d bytes  sha256=%s" % (artifact["name"], artifact["size"], artifact["sha256"]))
    print("  driver matrix: %s" % (matrix_path if matrix_path else "disabled"))
    print("  driver requirements: %d" % len(manifest["driver_requirements"]))
    print("  dependencies: %d" % len(manifest["dependencies"]))
    print("  signing: %s" % manifest["signing"]["status"])
    for warning in manifest["warnings"]:
        print("  warning: %s" % warning, file=sys.stderr)
    if dry_run:
        print("  would write:")
        print("    %s" % (out_dir / "SHA256SUMS"))
        print("    %s" % (out_dir / "release-manifest.json"))


def write_bundle(args, artifacts, manifest, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for artifact in artifacts:
        destination = out_dir / artifact["name"]
        shutil.copy2(artifact["source"], destination)
        copied_hash = sha256_file(destination)
        if copied_hash != artifact["sha256"]:
            raise RuntimeError(
                "hash changed while copying %s (source %s, copy %s)" % (artifact["name"], artifact["sha256"], copied_hash)
            )
    checksum_lines = ["%s  %s" % (artifact["sha256"], artifact["name"]) for artifact in artifacts]
    checksums_tmp = out_dir / "SHA256SUMS.tmp"
    with open(checksums_tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(checksum_lines) + "\n")
    os.replace(checksums_tmp, out_dir / "SHA256SUMS")
    manifest_tmp = out_dir / "release-manifest.json.tmp"
    with open(manifest_tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(manifest_tmp, out_dir / "release-manifest.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect release artifacts, write SHA256SUMS and release-manifest.json (dry-run by default).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", required=True, help="Release version, for example 5.2.0.")
    parser.add_argument("--name", default="Goo Engine", help="Project name.")
    parser.add_argument("--channel", default="production", choices=["production", "beta", "rc", "nightly"], help="Release channel.")
    parser.add_argument("--artifacts", action="append", default=[], metavar="DIR", help="Directory with build artifacts (repeatable).")
    parser.add_argument("--out", help="Release bundle directory (default: dist/<name>-<version>).")
    parser.add_argument("--commit", help="Source commit (default: GITHUB_SHA or unknown).")
    parser.add_argument("--branch", help="Source branch (default: GITHUB_REF_NAME or unknown).")
    parser.add_argument("--build-id", help="Build id (default: GITHUB_RUN_ID or a UTC timestamp).")
    parser.add_argument("--builder", help="Builder identity (default: GITHUB_ACTOR or the local user).")
    parser.add_argument("--dependency", action="append", default=[], metavar="NAME=VERSION", help="Declared dependency (repeatable).")
    parser.add_argument("--dependencies-file", help="Optional JSON list of dependencies.")
    parser.add_argument("--driver-matrix", default=str(DEFAULT_MATRIX), help="Driver matrix used for driver_requirements (empty string disables).")
    parser.add_argument("--windows-signed", action="store_true", help="Assert Windows artifacts were already signed.")
    parser.add_argument("--macos-signed", action="store_true", help="Assert macOS artifacts were already signed/notarized.")
    parser.add_argument("--apply", action="store_true", help="Actually write the bundle (default is dry-run).")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run even if --apply is also passed.")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version.strip() != args.version or not args.version:
        print("error: --version must be a non-empty version without spaces", file=sys.stderr)
        return 2
    if not args.artifacts:
        print("error: at least one --artifacts DIR is required", file=sys.stderr)
        return 2
    out_dir = Path(args.out).expanduser() if args.out else REPO_ROOT / "dist" / ("%s-%s" % (args.name.replace(" ", "-").lower(), args.version))
    matrix_path = Path(args.driver_matrix).expanduser() if args.driver_matrix else None

    warnings = []
    try:
        artifacts, scan_warnings = collect_artifacts(args.artifacts, out_dir)
        warnings.extend(scan_warnings)
        artifacts = dedupe_artifacts(artifacts)
        if not artifacts:
            print("error: no artifact files found below: %s" % ", ".join(args.artifacts), file=sys.stderr)
            return 2
        for artifact in artifacts:
            artifact["sha256"] = sha256_file(artifact["source"])
        driver_requirements = load_driver_requirements(matrix_path, warnings) if matrix_path else []
        dependencies = load_dependencies(
            Path(args.dependencies_file).expanduser() if args.dependencies_file else None,
            args.dependency,
            warnings,
        )
    except (OSError, ValueError) as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 2

    # Strip the non-serialisable source path before writing the manifest.
    public_artifacts = [
        {"path": artifact["name"], "size": artifact["size"], "sha256": artifact["sha256"]}
        for artifact in artifacts
    ]
    manifest, manifest_warnings = build_manifest(args, public_artifacts, "SHA256SUMS", driver_requirements, dependencies)
    warnings.extend(manifest_warnings)
    manifest["warnings"] = warnings

    dry_run = args.dry_run or not args.apply
    print_plan(args, artifacts, manifest, out_dir, matrix_path, dry_run)
    if dry_run:
        print("dry-run: no files written")
        return 0

    try:
        write_bundle(args, artifacts, manifest, out_dir)
    except (OSError, RuntimeError) as ex:
        print("error: failed to write release bundle: %s" % ex, file=sys.stderr)
        return 1
    print("release bundle written: %s" % out_dir)
    print("  %d artifact(s), SHA256SUMS, release-manifest.json" % len(artifacts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
