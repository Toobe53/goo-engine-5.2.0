#!/usr/bin/env python3
"""Build the per-user Windows installer reproducibly.

Inno Setup cannot open sources whose *effective* path (including a ``..``
prefix in the script) exceeds the legacy 260-character limit.  The install
tree itself can be under a long workspace path, so this wrapper exposes it
through a short directory junction for the duration of the compile.

This script only wraps ISCC; it does not build Blender and it does not sign.
The resulting executable is explicitly unsigned.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE = REPO_ROOT / "_install-windows2"
DEFAULT_OUTPUT = REPO_ROOT / "_work" / "release-staging"
DEFAULT_ISCC = Path(
    os.environ.get(
        "ISCC",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"),
    )
)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def default_iscc() -> Path:
    if DEFAULT_ISCC.is_file():
        return DEFAULT_ISCC
    for candidate in (
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ):
        if candidate.is_file():
            return candidate
    return DEFAULT_ISCC


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_junction(link: Path, target: Path) -> None:
    if link.exists():
        raise RuntimeError(f"junction already exists: {link}")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"mklink /J failed ({result.returncode}): {result.stdout.strip()} {result.stderr.strip()}"
        )


def remove_junction(link: Path) -> None:
    try:
        os.rmdir(link)
    except OSError:
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, check=False)


def source_proxy(source: Path) -> tuple[Path, Path | None, str | None]:
    """Return (path passed to ISCC, junction to clean up, warning)."""
    if os.name != "nt" or len(str(source)) < 80:
        return source, None, None
    # Keep the effective path short: C:\goo-iw-<8 hex>\<relative 142 chars>.
    for root in (Path("C:/"), Path(tempfile.gettempdir())):
        link = root / f"goo-iw-{uuid.uuid4().hex[:8]}"
        try:
            make_junction(link, source)
            return link, link, None
        except Exception as exc:  # try the next root, report if all fail
            warning = f"could not create short junction under {root}: {exc}"
            continue
    return source, None, warning


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="cmake --install tree")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--version", default="5.2.1")
    parser.add_argument("--iscc", type=Path, default=None)
    parser.add_argument("--script", type=Path, default=Path(__file__).with_name("goo-engine.iss"))
    parser.add_argument("--log", type=Path, default=None, help="ISCC output log (default: <output-dir>/iscc.log)")
    parser.add_argument("--no-short-path", action="store_true", help="pass the source path directly to ISCC")
    args = parser.parse_args(argv)

    version = args.version.strip()
    if not VERSION_RE.match(version):
        parser.error(f"--version must look like 5.2.1, got {version!r}")

    source = args.source.resolve()
    if not (source / "blender.exe").is_file():
        print(f"ERROR: {source / 'blender.exe'} not found; run cmake --install first", file=sys.stderr)
        return 2

    iscc = (args.iscc or default_iscc()).resolve()
    if not iscc.is_file():
        print(f"ERROR: ISCC.exe not found: {iscc}", file=sys.stderr)
        print("Install Inno Setup 6 or pass --iscc / set ISCC.", file=sys.stderr)
        return 2

    script = args.script.resolve()
    if not script.is_file():
        print(f"ERROR: installer script not found: {script}", file=sys.stderr)
        return 2

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = (args.log or (output_dir / "iscc.log")).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    junction: Path | None = None
    warning: str | None = None
    source_arg = source
    if not args.no_short_path:
        source_arg, junction, warning = source_proxy(source)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)

    output_name = f"goo-engine-{version}-windows-x64-setup.exe"
    output_path = output_dir / output_name
    started = time.time()
    try:
        cmd = [
            str(iscc),
            f"/DSourceDir={source_arg}",
            f"/DMyAppVersion={version}",
            f"/DOutputDir={output_dir}",
            str(script),
        ]
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            log.write("COMMAND: " + " ".join(cmd) + "\n\n")
            log.flush()
            result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False)
        elapsed = time.time() - started

        print(f"ISCC_EXIT={result.returncode}")
        print(f"ELAPSED_SECONDS={elapsed:.1f}")
        print(f"LOG={log_path}")
        if result.returncode != 0 or not output_path.is_file():
            print(f"ERROR: installer not produced: {output_path}", file=sys.stderr)
            if log_path.is_file():
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in lines[-20:]:
                    print(line, file=sys.stderr)
            return 1

        print(f"INSTALLER={output_path}")
        print(f"SIZE={output_path.stat().st_size}")
        print(f"SHA256={sha256_file(output_path)}")
        print("SIGNED=no")
        return 0
    finally:
        if junction is not None:
            remove_junction(junction)


if __name__ == "__main__":
    raise SystemExit(main())
