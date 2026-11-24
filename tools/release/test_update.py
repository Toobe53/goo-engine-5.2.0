#!/usr/bin/env python3
"""Unit tests for the release update manager (no real Blender required)."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update  # noqa: E402


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class UpdateManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="goo-update-test-")
        self.root = Path(self.tmp.name)
        self.install = self.root / "install"
        self.install.mkdir()
        (self.install / update.binary_name()).write_bytes(b"old-binary")
        (self.install / "old-only.txt").write_text("old", encoding="utf-8")
        self.state = self.root / "state.json"

        self.package = self.root / "release.zip"
        with zipfile.ZipFile(self.package, "w") as archive:
            archive.writestr("blender.exe" if update.os.name == "nt" else "blender", b"new-binary")
            archive.writestr("new-only.txt", "new")
        self.package_sha = sha256(self.package.read_bytes())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def args(self, **overrides):
        values = {
            "install_dir": self.install,
            "package": self.package,
            "expected_sha256": self.package_sha,
            "state": self.state,
            "dry_run": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_apply_and_rollback_swaps_trees(self) -> None:
        with mock.patch.object(update, "blender_version", return_value="5.2.1"):
            self.assertEqual(update.cmd_apply(self.args()), 0)
            state = update.load_state(self.state)
            self.assertEqual(state["status"], "applied")
            self.assertTrue(Path(state["backup"]).is_dir())
            self.assertTrue((self.install / "new-only.txt").is_file())
            self.assertFalse((self.install / "old-only.txt").exists())

            self.assertEqual(update.cmd_rollback(self.args()), 0)
        state = update.load_state(self.state)
        self.assertEqual(state["status"], "rolled_back")
        self.assertTrue((self.install / "old-only.txt").is_file())
        self.assertFalse((self.install / "new-only.txt").exists())

    def test_sha_mismatch_is_rejected(self) -> None:
        with mock.patch.object(update, "blender_version", return_value="5.2.1"):
            with self.assertRaises(update.UpdateError):
                update.cmd_apply(self.args(expected_sha256="0" * 64))
        self.assertTrue((self.install / "old-only.txt").is_file())

    def test_safe_member_rejects_escapes(self) -> None:
        self.assertTrue(update.safe_member("./blender.exe"))
        self.assertTrue(update.safe_member("sub/file.txt"))
        self.assertFalse(update.safe_member("../escape.txt"))
        self.assertFalse(update.safe_member("/absolute"))
        self.assertFalse(update.safe_member("C:/absolute"))

    def test_status_missing(self) -> None:
        args = self.args()
        args.command = "status"
        self.assertEqual(update.cmd_status(args), 1)

    def test_state_falls_back_when_parent_is_not_writable(self) -> None:
        local = self.root / "localappdata"
        with mock.patch.object(update, "_can_write", return_value=False):
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False):
                path = update.state_path_for(self.install)
        self.assertEqual(path.parent, local / "goo-engine" / "updates")
        self.assertTrue(path.name.startswith("install-"))
        self.assertTrue(path.name.endswith(".update-state.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
