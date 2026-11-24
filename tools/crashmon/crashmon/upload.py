"""Dry-run upload planning.

This prototype deliberately contains **no network client**. Supplying
``--upload-url`` together with ``--consent`` only produces a local
``upload-plan.json`` listing what would be sent. ``network_performed`` is always
false; the manifest validator rejects any bundle that claims otherwise.

A production implementation needs an authenticated HTTPS intake service, TLS
certificate validation, retry/backoff, checksums, per-file consent, and a
retention agreement. None of that exists here.
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .bundle import default_upload_section
from .util import (
    CrashmonError,
    atomic_write_json,
    ensure_dir,
    sha256_and_size,
    utc_iso,
)

UPLOAD_PLAN_SCHEMA = "crashmon.upload-plan/1"
UPLOAD_BOUNDARY = (
    "Dry-run only: this prototype performs no network I/O. With --upload-url and --consent it writes "
    "upload-plan.json locally and records the intended files, sizes, and hashes. Nothing leaves the machine."
)

_SENSITIVE_QUERY_KEYS = (
    "token",
    "key",
    "secret",
    "auth",
    "password",
    "signature",
    "sig",
    "credential",
    "session",
)


def sanitize_target(url: str) -> tuple[str, list[str]]:
    """Strip credentials from a URL before storing it in a bundle."""
    warnings: list[str] = []
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise CrashmonError("invalid --upload-url: {}".format(exc))
    if not parts.scheme or not parts.netloc:
        raise CrashmonError("--upload-url must be an absolute URL (scheme and host), got {!r}".format(url))
    if parts.scheme.lower() != "https":
        warnings.append("Upload target is not HTTPS; a production implementation must refuse plaintext uploads.")
    host = parts.hostname or ""
    port = parts.port
    netloc = host + (":{}".format(port) if port else "")
    if parts.username or parts.password:
        warnings.append("Credentials embedded in --upload-url were removed and never stored.")
        netloc = "<redacted>@" + netloc
    query_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    sanitized_pairs = []
    for key, value in query_pairs:
        if any(marker in key.lower() for marker in _SENSITIVE_QUERY_KEYS):
            sanitized_pairs.append((key, "<redacted>"))
            warnings.append("Query parameter {!r} was redacted from the stored target.".format(key))
        else:
            sanitized_pairs.append((key, value))
    query = urllib.parse.urlencode(sanitized_pairs)
    sanitized = urllib.parse.urlunsplit(
        (parts.scheme.lower(), netloc, parts.path or "/", query, "")
    )
    return sanitized, warnings


def _iter_bundle_files(bundle: Path, *, max_files: int) -> tuple[list[dict[str, Any]], list[str], int, bool]:
    files: list[dict[str, Any]] = []
    skipped: list[str] = []
    total_bytes = 0
    truncated = False
    for root, dirnames, filenames in os.walk(bundle):
        dirnames.sort()
        for filename in sorted(filenames):
            path = Path(root) / filename
            relative = path.relative_to(bundle).as_posix()
            if relative == "upload-plan.json":
                continue
            if path.is_symlink():
                skipped.append("{} (symlink)".format(relative))
                continue
            try:
                if not path.is_file():
                    skipped.append("{} (not a regular file)".format(relative))
                    continue
                digest, size = sha256_and_size(path)
            except OSError as exc:
                skipped.append("{} (unreadable: {})".format(relative, exc))
                continue
            if len(files) >= max_files:
                truncated = True
                break
            total_bytes += size
            files.append(
                {
                    "relative_path": relative,
                    "size_bytes": size,
                    "sha256": digest,
                }
            )
        if truncated:
            break
    return files, skipped, total_bytes, truncated


def build_upload_plan(
    bundle_dir: os.PathLike[str] | str,
    *,
    upload_url: str,
    consent: bool,
    max_files: int = 1000,
    manifest_relative: str = "manifest.json",
) -> dict[str, Any]:
    """Create ``upload-plan.json`` and return the manifest ``upload`` section."""
    bundle = Path(bundle_dir)
    if not bundle.is_dir():
        raise CrashmonError("bundle directory does not exist: {}".format(bundle))
    if not upload_url:
        return default_upload_section()
    target, warnings = sanitize_target(upload_url)
    if not consent:
        section = default_upload_section()
        section.update(
            {
                "requested": False,
                "consent": False,
                "mode": "blocked",
                "status": "consent-required",
                "target": target,
                "boundary": "Upload not planned: --consent is required in addition to --upload-url.",
                "warnings": warnings,
            }
        )
        return section

    files, skipped, total_bytes, truncated = _iter_bundle_files(bundle, max_files=max_files)
    if not any(entry["relative_path"] == manifest_relative for entry in files):
        skipped.append("{} (not written yet; the manifest section is produced before finalization)".format(manifest_relative))
    plan = {
        "plan_schema": UPLOAD_PLAN_SCHEMA,
        "created_utc": utc_iso(),
        "tool": {"name": "crashmon", "version": __version__, "prototype": True},
        "target": target,
        "dry_run": True,
        "network_performed": False,
        "consent": True,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "truncated": truncated,
        "files": files,
        "skipped": skipped,
        "warnings": warnings
        + [
            "This file is a local plan only. crashmon does not implement an upload transport.",
            "Review the listed files for personal or sensitive data before building a real client.",
        ],
        "boundary": UPLOAD_BOUNDARY,
    }
    plan_path = ensure_dir(bundle) / "upload-plan.json"
    atomic_write_json(plan_path, plan)
    return {
        "requested": True,
        "consent": True,
        "mode": "dry-run",
        "status": "planned-dry-run",
        "network_performed": False,
        "target": target,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "plan_file": "upload-plan.json",
        "warnings": warnings,
        "boundary": UPLOAD_BOUNDARY,
    }


def validate_upload_arguments(upload_url: Optional[str], consent: bool) -> None:
    """Reject inconsistent CLI upload flags before any bundle is created."""
    if upload_url and not consent:
        raise CrashmonError(
            "--upload-url requires --consent. Uploads are disabled by default and are dry-run only."
        )
    if consent and not upload_url:
        raise CrashmonError("--consent requires --upload-url; there is nothing to plan.")
