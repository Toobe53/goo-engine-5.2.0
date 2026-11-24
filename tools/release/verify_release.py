#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Goo Engine Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Verify a release bundle produced by build_release.py.

Checks performed by default:

* release-manifest.json is readable and has the required fields;
* SHA256SUMS lists exactly the manifest artifacts, with no duplicates;
* every artifact exists, matches the recorded size and SHA-256, and stays
  inside the release directory (no absolute paths or ``..`` traversal);
* the signing block is present and its placeholder state is self-consistent.

Signature *cryptographic* verification is optional and off by default, because
it requires platform tools (signtool / codesign / spctl) that release operators
may not have. Pass ``--check-signatures`` to invoke them where available, and
``--require-signed`` to reject unsigned placeholder bundles.

Exit codes: 0 = all checks passed, 1 = verification failure, 2 = usage or
manifest parsing error.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = "1.0"
CHECKSUM_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")
SIGNING_STATUSES = {"signed", "partial", "unsigned-placeholder"}
WINDOWS_SIGN_EXTENSIONS = {".exe", ".msi", ".dll", ".sys"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class Result:
    def __init__(self):
        self.checks = []
        self.failures = 0
        self.warnings = 0

    def add(self, name: str, ok: bool, detail: str = "", warning: bool = False) -> None:
        status = "pass" if ok else ("warning" if warning else "fail")
        if not ok and not warning:
            self.failures += 1
        if not ok and warning:
            self.warnings += 1
        self.checks.append({"name": name, "status": status, "detail": detail})

    def fail(self, name: str, detail: str) -> None:
        self.add(name, False, detail)

    def warn(self, name: str, detail: str) -> None:
        self.add(name, False, detail, warning=True)

    def ok(self, name: str, detail: str = "") -> None:
        self.add(name, True, detail)

    @property
    def passed(self) -> bool:
        return self.failures == 0


def parse_checksums(path: Path, result: Result) -> dict:
    entries = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as ex:
        result.fail("checksums.read", "cannot read %s: %s" % (path, ex))
        return entries
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = CHECKSUM_RE.match(line)
        if not match:
            result.fail("checksums.parse", "line %d is not '<sha256>  <path>': %r" % (lineno, line))
            continue
        digest, name = match.group(1).lower(), match.group(2)
        if name in entries:
            result.fail("checksums.duplicate", "duplicate entry for %r" % name)
            continue
        entries[name] = digest
    if not entries:
        result.fail("checksums.empty", "no checksum entries in %s" % path)
    return entries


def safe_target(release_dir: Path, name: str, result: Result, label: str) -> Path | None:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        result.fail(label, "unsafe artifact path: %r" % name)
        return None
    target = release_dir.joinpath(*pure.parts)
    try:
        target.resolve().relative_to(release_dir.resolve())
    except (OSError, ValueError):
        result.fail(label, "artifact path escapes release directory: %r" % name)
        return None
    return target


def check_manifest_structure(manifest: dict, result: Result) -> bool:
    if not isinstance(manifest, dict):
        result.fail("manifest.structure", "manifest root is not an object")
        return False
    for key in ("schema_version", "project", "source", "build", "artifacts", "checksums", "signing"):
        if key not in manifest:
            result.fail("manifest.structure", "missing field %r" % key)
    if result.failures:
        return False
    project = manifest.get("project") or {}
    source = manifest.get("source") or {}
    build = manifest.get("build") or {}
    if not project.get("version"):
        result.fail("manifest.version", "project.version is empty")
    else:
        result.ok("manifest.version", str(project.get("version")))
    commit = source.get("commit")
    if not commit or commit == "unknown":
        result.warn("manifest.commit", "source commit is unknown; provenance is incomplete")
    else:
        result.ok("manifest.commit", str(commit))
    if not build.get("id"):
        result.fail("manifest.build_id", "build.id is empty")
    else:
        result.ok("manifest.build_id", str(build.get("id")))
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        result.fail("manifest.artifacts", "artifacts must be a non-empty list")
        return False
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            result.fail("manifest.artifacts", "artifact %d is not an object" % index)
            continue
        for field in ("path", "sha256", "size"):
            if field not in artifact:
                result.fail("manifest.artifacts", "artifact %d is missing %r" % (index, field))
        digest = str(artifact.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            result.fail("manifest.artifacts", "artifact %r has an invalid sha256" % artifact.get("path"))
        if not isinstance(artifact.get("size"), int) or artifact.get("size", -1) < 0:
            result.fail("manifest.artifacts", "artifact %r has an invalid size" % artifact.get("path"))
    checksums = manifest.get("checksums") or {}
    if checksums.get("algorithm") != "sha256":
        result.fail("manifest.checksums", "checksums.algorithm must be sha256")
    if not checksums.get("file"):
        result.fail("manifest.checksums", "checksums.file is empty")
    return True


def check_signing(manifest: dict, result: Result, require_signed: bool) -> None:
    signing = manifest.get("signing")
    if not isinstance(signing, dict) or "status" not in signing:
        result.fail("signing.block", "signing block is missing or has no status")
        return
    status = signing.get("status")
    platforms = signing.get("platforms") or {}
    if status not in SIGNING_STATUSES:
        result.fail("signing.status", "unknown signing status %r" % status)
        return
    if status == "signed":
        unsigned_platforms = [name for name, item in platforms.items() if (item or {}).get("status") != "signed"]
        if unsigned_platforms:
            result.fail("signing.consistency", "overall signing is 'signed' but platforms are not: %s" % ", ".join(unsigned_platforms))
        else:
            result.ok("signing.status", "signed")
    elif require_signed:
        result.fail("signing.required", "signing status is %r; signed artifacts are required" % status)
    else:
        result.warn("signing.status", "signing status is %r (placeholder); use --require-signed to enforce" % status)
    for name in ("windows", "macos"):
        item = platforms.get(name)
        if not isinstance(item, dict) or not item.get("status"):
            result.warn("signing.platform.%s" % name, "missing signing placeholder for %s" % name)
    if status == "partial" and not require_signed:
        result.warn("signing.partial", "only some platforms are signed")


def find_signtool() -> str | None:
    candidate = os.environ.get("SIGTOOL_PATH")
    if candidate and Path(candidate).is_file():
        return candidate
    found = shutil.which("signtool")
    if found:
        return found
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
            if not base:
                continue
            kits = Path(base) / "Windows Kits" / "10" / "bin"
            if kits.is_dir():
                versions = sorted(kits.iterdir(), reverse=True)
                for version in versions:
                    for arch in ("x64", "x86"):
                        exe = version / arch / "signtool.exe"
                        if exe.is_file():
                            return str(exe)
    return None


def check_signatures(manifest: dict, release_dir: Path, result: Result) -> None:
    platform_name = platform.system().lower()
    artifacts = [artifact for artifact in manifest.get("artifacts", []) if isinstance(artifact, dict)]
    if platform_name == "windows":
        signtool = find_signtool()
        if not signtool:
            result.fail("signature.tool", "signtool not found; cannot verify Windows signatures")
            return
        checked = 0
        for artifact in artifacts:
            name = str(artifact.get("path"))
            if Path(name).suffix.lower() not in WINDOWS_SIGN_EXTENSIONS:
                continue
            target = safe_target(release_dir, name, result, "signature.path")
            if target is None or not target.is_file():
                continue
            checked += 1
            try:
                proc = subprocess.run(
                    [signtool, "verify", "/pa", "/q", str(target)],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=120,
                )
            except (OSError, subprocess.TimeoutExpired) as ex:
                result.fail("signature.%s" % name, "signtool failed: %s" % ex)
                continue
            if proc.returncode == 0:
                result.ok("signature.%s" % name, "signtool verify /pa")
            else:
                detail = (proc.stdout or proc.stderr or "").strip().splitlines()
                result.fail("signature.%s" % name, "signtool verify /pa exited %d: %s" % (proc.returncode, " ".join(detail[-3:])))
        if not checked:
            result.warn("signature.none", "no Windows-signable artifacts (.exe/.msi/.dll/.sys/.ps1) found")
    elif platform_name == "darwin":
        codesign = shutil.which("codesign")
        spctl = shutil.which("spctl")
        if not codesign:
            result.fail("signature.tool", "codesign not found; cannot verify macOS signatures")
            return
        checked = 0
        for artifact in artifacts:
            name = str(artifact.get("path"))
            suffix = Path(name).suffix.lower()
            target = safe_target(release_dir, name, result, "signature.path")
            if target is None or not target.is_file():
                continue
            if suffix == ".pkg":
                command = ["pkgutil", "--check-signature", str(target)]
            elif suffix == ".dmg" and spctl:
                command = [spctl, "-a", "-t", "open", "--context", "context:primary-signature", str(target)]
            elif suffix in (".dylib", ".so") or not suffix:
                command = [codesign, "--verify", "--strict", str(target)]
            else:
                continue
            checked += 1
            try:
                proc = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=120)
            except (OSError, subprocess.TimeoutExpired) as ex:
                result.fail("signature.%s" % name, "signature check failed: %s" % ex)
                continue
            if proc.returncode == 0:
                result.ok("signature.%s" % name, " ".join(command[:2]))
            else:
                detail = (proc.stdout or proc.stderr or "").strip().splitlines()
                result.fail("signature.%s" % name, "signature check exited %d: %s" % (proc.returncode, " ".join(detail[-3:])))
        if not checked:
            result.warn("signature.none", "no .pkg/.dmg/.dylib/.so artifacts found to verify")
    else:
        result.warn("signature.platform", "signature verification is not implemented for %s; checksums and placeholders still checked" % platform.system())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify SHA256SUMS, release-manifest.json and signing placeholders of a release bundle.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--release-dir", help="Release bundle directory (default: directory of --manifest or cwd).")
    parser.add_argument("--manifest", help="Manifest file (default: <release-dir>/release-manifest.json).")
    parser.add_argument("--checksums", help="Checksums file (default: <release-dir>/<manifest.checksums.file>).")
    parser.add_argument("--require-signed", action="store_true", help="Fail unless all declared platforms are signed.")
    parser.add_argument("--check-signatures", action="store_true", help="Invoke platform signature tools (signtool/codesign).")
    parser.add_argument("--json-out", help="Write a machine readable verification report to this path.")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.release_dir:
        release_dir = Path(args.release_dir).expanduser().resolve()
    elif args.manifest:
        release_dir = Path(args.manifest).expanduser().resolve().parent
    else:
        release_dir = Path.cwd()
    manifest_path = Path(args.manifest).expanduser().resolve() if args.manifest else release_dir / "release-manifest.json"

    result = Result()
    try:
        manifest = load_json(manifest_path)
    except (OSError, json.JSONDecodeError) as ex:
        print("error: cannot read manifest %s: %s" % (manifest_path, ex), file=sys.stderr)
        return 2
    if not isinstance(manifest, dict):
        print("error: manifest root is not a JSON object", file=sys.stderr)
        return 2

    if not check_manifest_structure(manifest, result):
        for check in result.checks:
            print("%-7s %-28s %s" % (check["status"], check["name"], check["detail"]))
        return 1

    checksums_name = str((manifest.get("checksums") or {}).get("file"))
    checksums_path = Path(args.checksums).expanduser() if args.checksums else release_dir / checksums_name
    checksums = parse_checksums(checksums_path, result)

    artifacts = [artifact for artifact in manifest.get("artifacts", []) if isinstance(artifact, dict)]
    manifest_paths = {}
    for artifact in artifacts:
        name = str(artifact.get("path"))
        if name in manifest_paths:
            result.fail("manifest.duplicate", "duplicate artifact path %r" % name)
            continue
        manifest_paths[name] = artifact

    missing_in_checksums = sorted(set(manifest_paths) - set(checksums))
    missing_in_manifest = sorted(set(checksums) - set(manifest_paths))
    if missing_in_checksums:
        result.fail("checksums.coverage", "not listed in checksums: %s" % ", ".join(missing_in_checksums))
    if missing_in_manifest:
        result.fail("checksums.extra", "listed in checksums but not in manifest: %s" % ", ".join(missing_in_manifest))
    if not missing_in_checksums and not missing_in_manifest:
        result.ok("checksums.coverage", "%d artifact(s)" % len(manifest_paths))

    if (manifest.get("checksums") or {}).get("count") != len(artifacts):
        result.fail("manifest.checksum_count", "checksums.count does not match the artifact list")
    else:
        result.ok("manifest.checksum_count", str(len(artifacts)))

    for name, artifact in sorted(manifest_paths.items()):
        target = safe_target(release_dir, name, result, "artifact.path")
        if target is None:
            continue
        if not target.is_file():
            result.fail("artifact.%s" % name, "file is missing")
            continue
        actual_size = target.stat().st_size
        if actual_size != artifact.get("size"):
            result.fail("artifact.%s" % name, "size mismatch: manifest %s, actual %d" % (artifact.get("size"), actual_size))
            continue
        try:
            actual_hash = sha256_file(target)
        except OSError as ex:
            result.fail("artifact.%s" % name, "cannot hash: %s" % ex)
            continue
        if actual_hash != str(artifact.get("sha256")).lower():
            result.fail("artifact.%s" % name, "sha256 mismatch: manifest %s, actual %s" % (artifact.get("sha256"), actual_hash))
            continue
        if name in checksums and checksums[name] != actual_hash:
            result.fail("artifact.%s" % name, "SHA256SUMS disagrees with the file")
            continue
        result.ok("artifact.%s" % name, "size and sha256 match")

    check_signing(manifest, result, args.require_signed)
    if args.check_signatures:
        check_signatures(manifest, release_dir, result)

    report = {
        "schema_version": "1.0",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest": str(manifest_path),
        "release_dir": str(release_dir),
        "passed": result.passed,
        "failures": result.failures,
        "warnings": result.warnings,
        "checks": result.checks,
    }
    if args.json_out:
        try:
            Path(args.json_out).expanduser().write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as ex:
            print("warning: cannot write %s: %s" % (args.json_out, ex), file=sys.stderr)

    for check in result.checks:
        print("%-7s %-28s %s" % (check["status"], check["name"], check["detail"]))
    print("")
    print("verification: %s (%d failure(s), %d warning(s))" % ("PASS" if result.passed else "FAIL", result.failures, result.warnings))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
