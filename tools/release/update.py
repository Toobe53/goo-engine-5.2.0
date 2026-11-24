#!/usr/bin/env python3
"""Apply and roll back a Goo Engine release on a local installation.

The manager treats a release as a tree containing ``blender.exe`` (Windows)
or ``blender`` (Linux).  ``apply`` validates the SHA-256, extracts the package
next to the installation, renames the current tree to a backup, and swaps the
new tree in.  ``rollback`` restores the most recent backup.

A Windows setup executable can also be applied with ``--installer``: the
manager backs up the current tree and runs the installer silently into it.
No code is downloaded here; the caller supplies the package and, whenever
possible, its expected digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

SCHEMA = 1
VERSION_RE = re.compile(r"Blender\s+([0-9][0-9A-Za-z.+-]*)")
ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar")


class UpdateError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def binary_name() -> str:
    return "blender.exe" if os.name == "nt" else "blender"


def find_binary(root: Path) -> Path | None:
    direct = root / binary_name()
    if direct.is_file():
        return direct
    candidates = sorted(root.glob(f"*/{binary_name()}"))
    if len(candidates) == 1 and candidates[0].is_file():
        return candidates[0]
    return None


def blender_version(binary: Path, timeout: int = 30) -> str:
    env = os.environ.copy()
    env.setdefault("BLENDER_USER_CONFIG", str(Path(tempfile.gettempdir()) / "goo-update-empty-config"))
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"cannot run {binary}: {exc}") from exc
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    match = VERSION_RE.search(text)
    if result.returncode != 0 or not match:
        raise UpdateError(f"{binary} --version failed (exit {result.returncode}): {text.strip()[:300]}")
    return match.group(1)


def _can_write(directory: Path) -> bool:
    probe = directory / f".goo-write-{os.getpid()}-{time.time_ns()}"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        try:
            probe.unlink()
        except OSError:
            pass
        return False


def fallback_state_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or tempfile.gettempdir()
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "goo-engine" / "updates"


def state_path_for(install_dir: Path) -> Path:
    sibling = install_dir.parent / f"{install_dir.name}.update-state.json"
    if _can_write(install_dir.parent):
        return sibling
    # Root-level or otherwise read-only parents cannot hold the state file.
    # Keep a stable per-install entry under the user state directory instead.
    key = hashlib.sha256(str(install_dir.resolve()).lower().encode("utf-8")).hexdigest()[:12]
    return fallback_state_dir() / f"{install_dir.name}-{key}.update-state.json"


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"cannot read state {path}: {exc}") from exc
    if data.get("schema") != SCHEMA:
        raise UpdateError(f"unsupported state schema in {path}")
    return data


def write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def safe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return False
    parts = PurePosixPath(normalized).parts
    return ".." not in parts


def extract_zip(package: Path, dest: Path) -> None:
    with zipfile.ZipFile(package) as archive:
        for info in archive.infolist():
            if not safe_member(info.filename):
                raise UpdateError(f"unsafe zip member: {info.filename}")
        archive.extractall(dest)


def extract_tar(package: Path, dest: Path) -> None:
    with tarfile.open(package, "r:*") as archive:
        for member in archive.getmembers():
            if not safe_member(member.name):
                raise UpdateError(f"unsafe tar member: {member.name}")
            if member.issym() or member.islnk():
                target = PurePosixPath(member.linkname)
                if member.linkname.startswith("/") or ".." in target.parts:
                    raise UpdateError(f"unsafe tar link: {member.name} -> {member.linkname}")
        # Python 3.12 supports filter="data"; keep compatibility with older 3.x.
        try:
            archive.extractall(dest, filter="data")  # type: ignore[call-arg]
        except TypeError:
            archive.extractall(dest)


def extract_package(package: Path, dest: Path) -> None:
    lower = package.name.lower()
    if lower.endswith(".zip"):
        extract_zip(package, dest)
    elif lower.endswith(ARCHIVE_SUFFIXES[1:]):
        extract_tar(package, dest)
    else:
        raise UpdateError(f"unsupported package type: {package.name}")


def package_root(staging: Path) -> Path:
    binary = find_binary(staging)
    if binary is None:
        raise UpdateError(f"no {binary_name()} in extracted package {staging}")
    return binary.parent


def make_backup(install_dir: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = install_dir.parent / f"{install_dir.name}.backup-{stamp}"
    if backup.exists():
        raise UpdateError(f"backup already exists: {backup}")
    shutil.move(str(install_dir), str(backup))
    return backup


def restore_backup(install_dir: Path, backup: Path) -> None:
    if not backup.is_dir():
        raise UpdateError(f"backup missing: {backup}")
    failed = install_dir.parent / f"{install_dir.name}.failed-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    if install_dir.exists():
        shutil.move(str(install_dir), str(failed))
    shutil.move(str(backup), str(install_dir))


def run_installer(package: Path, install_dir: Path, log_path: Path) -> None:
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Inno Setup can hang when launched from a deep workspace path on Windows.
    # Stage the executable on a short temp path; the package is not modified.
    with tempfile.TemporaryDirectory(prefix="goo-installer-") as temp_dir:
        staged = Path(temp_dir) / package.name
        shutil.copy2(package, staged)
        cmd = [
            str(staged),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            f"/DIR={install_dir}",
            f"/LOG={log_path}",
        ]
        result = subprocess.run(cmd, timeout=900, check=False)
    if result.returncode != 0:
        raise UpdateError(f"installer failed with exit {result.returncode}; log: {log_path}")


def cmd_status(args: argparse.Namespace) -> int:
    install_dir = args.install_dir.resolve()
    state_path = args.state or state_path_for(install_dir)
    if not state_path.is_file():
        print(f"STATE=missing path={state_path}")
        return 1
    data = load_state(state_path)
    print(f"STATE={data.get('status')}")
    print(f"INSTALL_DIR={data.get('install_dir')}")
    print(f"PACKAGE={data.get('package')}")
    print(f"VERSION={data.get('new_version')}")
    print(f"PREVIOUS_VERSION={data.get('previous_version')}")
    print(f"BACKUP={data.get('backup')}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    install_dir = args.install_dir.resolve()
    package = args.package.resolve()
    if not package.is_file():
        raise UpdateError(f"package not found: {package}")
    if args.expected_sha256:
        actual = sha256_file(package)
        if actual.lower() != args.expected_sha256.lower():
            raise UpdateError(f"sha256 mismatch: expected {args.expected_sha256}, got {actual}")
        print(f"SHA256={actual}")

    installer_mode = package.suffix.lower() == ".exe"
    if installer_mode and os.name != "nt":
        raise UpdateError("installer packages are only supported on Windows")

    old_version = None
    if install_dir.is_dir():
        old_binary = find_binary(install_dir)
        if old_binary:
            old_version = blender_version(old_binary)
    elif not installer_mode:
        raise UpdateError(f"install dir does not exist: {install_dir}")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    staging = install_dir.parent / f"{install_dir.name}.staging-{stamp}"
    backup: Path | None = None
    state_path = args.state or state_path_for(install_dir)

    if args.dry_run:
        print(f"PLAN=apply install_dir={install_dir} package={package}")
        print(f"PLAN_MODE={'installer' if installer_mode else 'archive'}")
        print(f"PLAN_PREVIOUS_VERSION={old_version or 'none'}")
        print(f"PLAN_STATE={state_path}")
        return 0

    try:
        if installer_mode:
            if install_dir.exists():
                backup = make_backup(install_dir)
                print(f"BACKUP={backup}")
            log_path = package.parent / f"{install_dir.name}.installer-{stamp}.log"
            run_installer(package, install_dir, log_path)
            new_binary = find_binary(install_dir)
            if new_binary is None:
                raise UpdateError("installer completed but blender binary is missing")
            new_version = blender_version(new_binary)
            print(f"INSTALL_LOG={log_path}")
        else:
            staging.mkdir(parents=True)
            extract_package(package, staging)
            root = package_root(staging)
            new_version = blender_version(find_binary(root) or (root / binary_name()))
            if install_dir.exists():
                backup = make_backup(install_dir)
                print(f"BACKUP={backup}")
            shutil.move(str(root), str(install_dir))
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            new_binary = find_binary(install_dir)
            if new_binary is None:
                raise UpdateError("new install has no blender binary")

        data = {
            "schema": SCHEMA,
            "status": "applied",
            "install_dir": str(install_dir),
            "package": str(package),
            "package_sha256": sha256_file(package),
            "previous_version": old_version,
            "new_version": new_version,
            "backup": str(backup) if backup else None,
            "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        write_state(state_path, data)
        print(f"VERSION={new_version}")
        print(f"STATE={state_path}")
        print("RESULT=APPLIED")
        return 0
    except Exception:
        if backup and backup.is_dir():
            if install_dir.exists():
                shutil.rmtree(install_dir, ignore_errors=True)
            shutil.move(str(backup), str(install_dir))
            print(f"RESTORED={install_dir}", file=sys.stderr)
        raise


def cmd_rollback(args: argparse.Namespace) -> int:
    install_dir = args.install_dir.resolve()
    state_path = args.state or state_path_for(install_dir)
    data = load_state(state_path)
    backup = Path(data.get("backup") or "")
    if not data.get("backup") or not backup.is_dir():
        raise UpdateError(f"no usable backup in {state_path}: {backup}")
    if args.dry_run:
        print(f"PLAN=rollback install_dir={install_dir} backup={backup}")
        return 0
    restore_backup(install_dir, backup)
    data["status"] = "rolled_back"
    data["rolled_back_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_state(state_path, data)
    binary = find_binary(install_dir)
    version = blender_version(binary) if binary else "unknown"
    print(f"VERSION={version}")
    print(f"STATE={state_path}")
    print("RESULT=ROLLED_BACK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    commons = argparse.ArgumentParser(add_help=False)
    commons.add_argument("--install-dir", type=Path, required=True)
    commons.add_argument("--state", type=Path, default=None)
    commons.add_argument("--dry-run", action="store_true")

    apply = sub.add_parser("apply", parents=[commons], help="apply a zip/tar.gz tree or Windows setup")
    apply.add_argument("--package", type=Path, required=True)
    apply.add_argument("--expected-sha256", default=None)
    apply.set_defaults(func=cmd_apply)

    rollback = sub.add_parser("rollback", parents=[commons], help="restore the last backup")
    rollback.set_defaults(func=cmd_rollback)

    status = sub.add_parser("status", parents=[commons], help="show the recorded update state")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except UpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
