"""Unit tests for the crashmon prototype.

Run from ``tools/crashmon`` with::

    python -m unittest discover -s tests -v

The tests use only the standard library and never contact the network. Platform
specific collectors are exercised through their dependency-injection hooks so
the Linux capability path can be tested on Windows/macOS too.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from crashmon import SCHEMA_VERSION  # noqa: E402
from crashmon import collectors, report, upload  # noqa: E402
from crashmon.bundle import (  # noqa: E402
    create_bundle,
    finalize_manifest,
    load_manifest,
    make_manifest,
    validate_manifest,
)
from crashmon.redaction import Redactor  # noqa: E402
from crashmon.runner import RotatingTextWriter, classify_returncode, run_supervised  # noqa: E402
from crashmon.util import CrashmonError  # noqa: E402


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def make_bundle(self, kind: str = "run") -> Path:
        return create_bundle(self.tmp / "bundles", kind)

    def make_manifest(self, run=None, bundle: Path = None):
        bundle = bundle or self.make_bundle()
        redactor = Redactor(
            "basic",
            username="alice",
            home=self.tmp / "home",
            hostname="test-host",
        )
        manifest = make_manifest(kind="run", bundle_dir=bundle, redactor=redactor, run=run)
        final = finalize_manifest(bundle, manifest, redactor)
        return bundle, final, redactor

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "crashmon", *args],
            cwd=str(TOOL_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class RedactionTests(unittest.TestCase):
    def test_basic_identifiers_are_replaced(self) -> None:
        home = r"C:\Users\alice"
        redactor = Redactor("basic", username="alice", home=home, hostname="workstation-7")
        text = "home={} user=alice host=workstation-7".format(home)
        out = redactor.redact(text)
        self.assertIn("<HOME>", out)
        self.assertIn("<USER>", out)
        self.assertIn("<HOSTNAME>", out)
        self.assertNotIn("alice", out.lower())
        self.assertNotIn("workstation-7", out)
        self.assertGreaterEqual(redactor.replacements, 3)
        self.assertTrue(redactor.applied)
        self.assertEqual(redactor.summary()["level"], "basic")

    def test_username_does_not_match_inside_words(self) -> None:
        redactor = Redactor("basic", username="bob", home="/home/bob", hostname="bob-host")
        out = redactor.redact("bobby visited bob-host with bob")
        self.assertIn("bobby", out)
        self.assertNotIn("with bob", out)
        self.assertNotIn("bob-host", out)

    def test_strict_redacts_email_and_ipv4(self) -> None:
        redactor = Redactor("strict", username="alice", home="/home/alice", hostname="host-1")
        out = redactor.redact("contact alice@example.com from 192.168.10.4")
        self.assertIn("<EMAIL>", out)
        self.assertIn("<IP>", out)
        self.assertNotIn("alice@example.com", out)
        self.assertNotIn("192.168.10.4", out)

    def test_none_level_leaves_text_untouched(self) -> None:
        redactor = Redactor("none")
        text = "user alice at C:\\Users\\alice on host-1"
        self.assertEqual(redactor.redact(text), text)
        self.assertFalse(redactor.applied)
        self.assertEqual(redactor.replacements, 0)
        self.assertEqual(Redactor("none").summary()["placeholders"], [])

    def test_redact_obj_recurses_lists_and_dicts(self) -> None:
        redactor = Redactor("basic", username="alice", home="/home/alice", hostname="host-1")
        value = {
            "path": "/home/alice/project",
            "items": ["alice", {"host": "host-1"}],
            "count": 3,
        }
        out = redactor.redact_obj(value)
        self.assertEqual(out["path"], "<HOME>/project")
        self.assertEqual(out["items"][0], "<USER>")
        self.assertEqual(out["items"][1]["host"], "<HOSTNAME>")
        self.assertEqual(out["count"], 3)

    def test_invalid_level_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Redactor("paranoid")


# ---------------------------------------------------------------------------
# Exit-code classification
# ---------------------------------------------------------------------------


class ExitCodeTests(unittest.TestCase):
    def assertCategory(self, result, category: str) -> None:
        self.assertEqual(result["category"], category, result)

    def test_windows_success(self) -> None:
        self.assertCategory(classify_returncode(0, system="Windows"), "success")

    def test_windows_access_violation_signed_and_unsigned(self) -> None:
        unsigned = classify_returncode(0xC0000005, system="Windows")
        self.assertCategory(unsigned, "crash")
        self.assertEqual(unsigned["windows_exception"]["name"], "ACCESS_VIOLATION")
        self.assertEqual(unsigned["code_hex"], "0xC0000005")
        signed = classify_returncode(-1073741819, system="Windows")
        self.assertCategory(signed, "crash")
        self.assertEqual(signed["code_hex"], "0xC0000005")

    def test_windows_known_statuses(self) -> None:
        self.assertCategory(classify_returncode(0xC0000409, system="Windows"), "crash")
        self.assertCategory(classify_returncode(0xC00000FD, system="Windows"), "crash")
        self.assertCategory(classify_returncode(0x80000003, system="Windows"), "crash")
        self.assertCategory(classify_returncode(0xC0000135, system="Windows"), "error")
        self.assertCategory(classify_returncode(0xC000013A, system="Windows"), "interrupted")

    def test_windows_unknown_statuses(self) -> None:
        status = classify_returncode(0xC0DE0000, system="Windows")
        self.assertCategory(status, "crash")
        self.assertEqual(status["windows_exception"]["name"], "UNKNOWN_NTSTATUS")
        warning = classify_returncode(0x80001234, system="Windows")
        self.assertCategory(warning, "exception")

    def test_windows_plain_error_code(self) -> None:
        result = classify_returncode(7, system="Windows")
        self.assertCategory(result, "error")
        self.assertIsNone(result["windows_exception"])

    def test_posix_signals(self) -> None:
        segv = classify_returncode(-11, system="Linux")
        self.assertCategory(segv, "crash")
        self.assertEqual(segv["signal"], 11)
        self.assertEqual(segv["signal_name"], "SIGSEGV")
        self.assertCategory(classify_returncode(-9, system="Linux"), "killed")
        self.assertCategory(classify_returncode(-15, system="Linux"), "terminated")
        self.assertCategory(classify_returncode(3, system="Linux"), "error")

    def test_timeout_takes_precedence_over_exit_code(self) -> None:
        result = classify_returncode(0, system="Windows", timed_out=True)
        self.assertCategory(result, "timeout")
        self.assertTrue(result["timed_out"])

    def test_missing_exit_status_is_spawn_error(self) -> None:
        self.assertCategory(classify_returncode(None, system="Linux"), "spawn-error")


# ---------------------------------------------------------------------------
# Manifest schema and bundle lifecycle
# ---------------------------------------------------------------------------


class ManifestTests(TempDirTestCase):
    def test_valid_manifest_roundtrip(self) -> None:
        bundle, final, _ = self.make_manifest()
        self.assertEqual(final["schema_version"], SCHEMA_VERSION)
        self.assertEqual(validate_manifest(final), [])
        self.assertTrue((bundle / "manifest.json").is_file())
        loaded, problems = load_manifest(bundle)
        self.assertEqual(problems, [])
        self.assertEqual(loaded["bundle"]["id"], bundle.name)
        self.assertEqual(loaded["environment"]["scope"], "allowlist")
        self.assertLessEqual(set(loaded["environment"]["values"]), set(loaded["environment"]["considered_keys"]))

    def test_manifest_with_run_and_logs_roundtrip(self) -> None:
        bundle = self.make_bundle()
        redactor = Redactor("basic", username="alice", home=self.tmp / "home", hostname="test-host")
        log_path = bundle / "logs" / "stdout.log"
        log_path.write_text("hello alice\n", encoding="utf-8")
        digest = hashlib.sha256(log_path.read_bytes()).hexdigest()
        run = {
            "started_utc": "2026-01-01T00:00:00Z",
            "ended_utc": "2026-01-01T00:00:01Z",
            "duration_s": 1.0,
            "command": [str(self.tmp / "fake-blender.exe")],
            "exit": classify_returncode(0xC0000005, system="Windows"),
            "logs": [],
            "spawn_error": None,
        }
        logs = [
            {
                "stream": "stdout",
                "relative_path": "logs/stdout.log",
                "size_bytes": log_path.stat().st_size,
                "sha256": digest,
                "retained": True,
                "tail_only": False,
                "tail": ["hello <USER>"],
            }
        ]
        manifest = make_manifest(kind="run", bundle_dir=bundle, redactor=redactor, run=run, logs=logs)
        final = finalize_manifest(bundle, manifest, redactor)
        self.assertEqual(validate_manifest(final), [])
        self.assertIn("<USER>", final["logs"][0]["tail"][0])
        self.assertEqual(final["run"]["exit"]["category"], "crash")

    def test_invalid_manifest_reports_all_problems(self) -> None:
        _, valid, _ = self.make_manifest()
        bad = copy.deepcopy(valid)
        bad["schema_version"] = "wrong/0"
        del bad["environment"]
        del bad["blender"]
        bad["logs"] = [
            {
                "stream": "stdout",
                "relative_path": "../escape.log",
                "sha256": "not-a-hash",
                "retained": True,
                "tail": [],
            }
        ]
        bad["crash_artifacts"] = [
            {
                "source": "test",
                "kind": "minidump",
                "relative_path": "C:\\absolute.dmp",
                "sha256": "zzz",
                "copied": True,
                "size_bytes": "large",
            }
        ]
        bad["upload"] = {"requested": True, "consent": False, "network_performed": True}
        problems = "\n".join(validate_manifest(bad))
        for fragment in (
            "schema_version must be",
            "missing top-level key: environment",
            "missing top-level key: blender",
            "logs[0].relative_path is invalid",
            "logs[0].sha256 is invalid",
            "crash_artifacts[0].relative_path is invalid",
            "crash_artifacts[0].sha256 is invalid",
            "crash_artifacts[0].size_bytes must be an integer or null",
            "upload.network_performed must be false",
            "upload.requested requires upload.consent",
        ):
            self.assertIn(fragment, problems)

    def test_non_object_manifest_is_rejected(self) -> None:
        self.assertEqual(validate_manifest([]), ["manifest is not a JSON object"])
        self.assertEqual(validate_manifest("nope"), ["manifest is not a JSON object"])

    def test_create_bundle_layout_and_unique_names(self) -> None:
        first = create_bundle(self.tmp / "out", "collect")
        second = create_bundle(self.tmp / "out", "collect")
        self.assertNotEqual(first, second)
        self.assertTrue(first.name.startswith("crashmon-collect-"))
        self.assertTrue((first / "logs").is_dir())
        self.assertTrue((first / "crash").is_dir())
        named = create_bundle(self.tmp / "out", "run", "My Custom Bundle")
        self.assertTrue(named.name.startswith("crashmon-run-"))
        self.assertNotIn(" ", named.name)

    def test_load_manifest_missing_bundle_raises(self) -> None:
        with self.assertRaises(CrashmonError):
            load_manifest(self.tmp / "does-not-exist")


# ---------------------------------------------------------------------------
# Artifact collection and import
# ---------------------------------------------------------------------------


class CollectorTests(TempDirTestCase):
    def test_process_matching(self) -> None:
        self.assertTrue(collectors.process_matches(r"C:\dumps\blender.exe.1234.dmp", "blender"))
        self.assertTrue(collectors.process_matches("Blender", "blender.exe"))
        self.assertFalse(collectors.process_matches(r"C:\dumps\explorer.exe.1.dmp", "blender"))
        self.assertTrue(collectors.process_matches("anything", "*"))

    def test_within_window(self) -> None:
        self.assertTrue(collectors.within_window(100.0, None))
        self.assertTrue(collectors.within_window(100.0, 50.0))
        self.assertFalse(collectors.within_window(10.0, 50.0))
        self.assertFalse(collectors.within_window(None, 50.0))

    def test_materialize_binary_and_text_artifacts(self) -> None:
        bundle = create_bundle(self.tmp / "bundles", "run")
        binary = self.tmp / "blender-1234.dmp"
        binary.write_bytes(b"MZ fake minidump \x00\x01")
        text = self.tmp / "blender-report.wer"
        text.write_text("user alice home C:\\Users\\alice\n", encoding="utf-8")
        redactor = Redactor("basic", username="alice", home="C:\\Users\\alice", hostname="host")

        entries = collectors.materialize_candidates(
            [
                {"source": "test", "path": str(binary), "kind": "minidump"},
                {"source": "test", "path": str(text), "kind": "wer-report", "text": True},
            ],
            bundle,
            redactor,
        )
        self.assertEqual(len(entries), 2)
        by_kind = {entry["kind"]: entry for entry in entries}
        binary_entry = by_kind["minidump"]
        self.assertTrue(binary_entry["copied"])
        self.assertTrue(binary_entry["relative_path"].startswith("crash/"))
        copied = bundle / binary_entry["relative_path"]
        self.assertEqual(copied.read_bytes(), binary.read_bytes())
        self.assertEqual(binary_entry["sha256"], hashlib.sha256(binary.read_bytes()).hexdigest())

        text_entry = by_kind["wer-report"]
        self.assertTrue(text_entry["copied"])
        self.assertTrue(text_entry["text_redacted"])
        stored_text = (bundle / text_entry["relative_path"]).read_text(encoding="utf-8")
        self.assertNotIn("alice", stored_text)
        self.assertIn("<USER>", stored_text)
        self.assertNotEqual(text_entry["sha256"], text_entry["original_sha256"])

    def test_materialize_skips_oversized_and_excess_artifacts(self) -> None:
        bundle = create_bundle(self.tmp / "bundles", "run")
        big = self.tmp / "big.dmp"
        big.write_bytes(b"x" * 100)
        small = self.tmp / "small.dmp"
        small.write_bytes(b"x" * 10)
        redactor = Redactor("none")
        entries = collectors.materialize_candidates(
            [{"source": "test", "path": str(big), "kind": "minidump"}],
            bundle,
            redactor,
            max_artifact_bytes=50,
        )
        self.assertFalse(entries[0]["copied"])
        self.assertIn("exceeds", entries[0]["note"])

        entries = collectors.materialize_candidates(
            [
                {"source": "test", "path": str(big), "kind": "minidump"},
                {"source": "test", "path": str(small), "kind": "minidump"},
            ],
            bundle,
            redactor,
            max_artifacts=1,
        )
        self.assertEqual(sum(1 for entry in entries if entry["copied"]), 1)
        # Stable source/path ordering copies big.dmp first; small.dmp is skipped.
        skipped = [entry for entry in entries if not entry["copied"]]
        self.assertEqual(len(skipped), 1)
        self.assertIn("max-artifacts", skipped[0]["note"])

    def test_import_log_file_truncates_and_redacts(self) -> None:
        bundle = create_bundle(self.tmp / "bundles", "collect")
        log = self.tmp / "blender.log"
        log.write_text("alice line\n" * 200, encoding="utf-8")
        redactor = Redactor("basic", username="alice", home=self.tmp / "home", hostname="host")
        entry = collectors.import_log_file(log, bundle, redactor, max_bytes=64, tail_lines=5)
        self.assertTrue(entry["truncated"])
        self.assertTrue(entry["retained"])
        self.assertLessEqual(len(entry["tail"]), 5)
        stored = bundle / entry["relative_path"]
        content = stored.read_text(encoding="utf-8")
        self.assertNotIn("alice", content)
        self.assertIn("<USER>", content)
        self.assertEqual(entry["sha256"], hashlib.sha256(stored.read_bytes()).hexdigest())

    def test_linux_capabilities_do_not_crash_without_coredumpctl(self) -> None:
        info = collectors.linux_platform_info(
            which_func=lambda name: None,
            runner=lambda *args, **kwargs: self.fail("coredumpctl must not run when it is not installed"),
            core_pattern_reader=lambda: (None, "core_pattern unavailable in test"),
            core_limit_getter=lambda: {"soft": 0, "hard": 0, "unit": "blocks", "error": None},
            systemd_coredump_dir=str(self.tmp / "missing-coredump-dir"),
        )
        self.assertFalse(info["coredumpctl"]["available"])
        self.assertFalse(info["core_pattern"]["readable"])
        self.assertFalse(info["available"])
        self.assertTrue(any("core_pattern" in item for item in info["errors"]))
        self.assertIn("available", info)

    def test_discover_generic_crash_directory_filters_by_process(self) -> None:
        crash_dir = self.tmp / "crashes"
        crash_dir.mkdir()
        matching = crash_dir / "blender.exe.1234.dmp"
        matching.write_bytes(b"dump")
        unrelated = crash_dir / "explorer.exe.9.dmp"
        unrelated.write_bytes(b"dump")
        candidates = collectors.discover_generic_crash_dirs([crash_dir], "blender", None)
        self.assertEqual(len(candidates), 1)
        self.assertIn("blender", str(candidates[0]["path"]).lower())


# ---------------------------------------------------------------------------
# Supervisor and log rotation
# ---------------------------------------------------------------------------


class RunnerTests(TempDirTestCase):
    def test_rotation_and_backups(self) -> None:
        path = self.tmp / "logs" / "stdout.log"
        writer = RotatingTextWriter(path, max_bytes=8, backups=2)
        for chunk in ("aaa\n", "bbb\n", "ccc\n", "ddd\n"):
            writer.write_text(chunk)
        writer.close()
        self.assertTrue(writer.rotated)
        self.assertEqual(path.read_text(encoding="utf-8"), "ccc\nddd\n")
        self.assertTrue(Path(str(path) + ".1").is_file())
        self.assertEqual(Path(str(path) + ".1").read_text(encoding="utf-8"), "aaa\nbbb\n")
        self.assertGreaterEqual(len(writer.files()), 2)

    def test_tail_only_writer_stores_nothing(self) -> None:
        path = self.tmp / "logs" / "stdout.log"
        writer = RotatingTextWriter(path, max_bytes=100, retain=False, redactor=Redactor("none"))
        returned = writer.write_text("hello\n")
        writer.close()
        self.assertEqual(returned, "hello\n")
        self.assertEqual(writer.total_bytes, 6)
        self.assertFalse(path.exists())

    def test_run_supervised_captures_output_and_exit(self) -> None:
        bundle = create_bundle(self.tmp / "bundles", "run")
        script = (
            "import sys\n"
            "print('crashmon-test-out')\n"
            "sys.stderr.write('crashmon-test-err\\n')\n"
            "raise SystemExit(3)\n"
        )
        result = run_supervised(
            [sys.executable, "-c", script],
            bundle,
            redactor=Redactor("none"),
            echo=False,
            tail_lines=10,
        )
        self.assertIsNone(result["spawn_error"])
        self.assertEqual(result["exit"]["raw"], 3)
        self.assertEqual(result["exit"]["category"], "error")
        self.assertEqual(len(result["logs"]), 2)
        by_stream = {entry["stream"]: entry for entry in result["logs"]}
        self.assertIn("crashmon-test-out", by_stream["stdout"]["tail"])
        self.assertIn("crashmon-test-err", by_stream["stderr"]["tail"])
        for entry in result["logs"]:
            stored = bundle / entry["relative_path"]
            self.assertTrue(stored.is_file())
            self.assertEqual(entry["sha256"], hashlib.sha256(stored.read_bytes()).hexdigest())
        self.assertGreaterEqual(result["duration_s"], 0)

    def test_run_supervised_timeout(self) -> None:
        bundle = create_bundle(self.tmp / "bundles", "run")
        result = run_supervised(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            bundle,
            redactor=Redactor("none"),
            echo=False,
            timeout=0.5,
        )
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["exit"]["category"], "timeout")
        self.assertEqual(result["timeout_seconds"], 0.5)

    def test_run_supervised_spawn_error(self) -> None:
        bundle = create_bundle(self.tmp / "bundles", "run")
        missing = self.tmp / "definitely-not-an-executable" / "blender"
        result = run_supervised([str(missing)], bundle, redactor=Redactor("none"), echo=False)
        self.assertIsNotNone(result["spawn_error"])
        self.assertEqual(result["exit"]["category"], "spawn-error")
        self.assertEqual(result["logs"], [])


# ---------------------------------------------------------------------------
# Dry-run upload planning
# ---------------------------------------------------------------------------


class UploadTests(TempDirTestCase):
    def test_flag_validation(self) -> None:
        with self.assertRaises(CrashmonError):
            upload.validate_upload_arguments("https://example.com/u", False)
        with self.assertRaises(CrashmonError):
            upload.validate_upload_arguments(None, True)
        upload.validate_upload_arguments(None, False)
        upload.validate_upload_arguments("https://example.com/u", True)

    def test_target_sanitization(self) -> None:
        target, warnings = upload.sanitize_target(
            "http://user:pass@example.com:8080/intake?token=abc&build=42"
        )
        self.assertIn("example.com:8080", target)
        self.assertIn("build=42", target)
        self.assertNotIn("abc", target)
        self.assertNotIn("user:pass", target)
        self.assertIn("<redacted>@", target)
        self.assertTrue(any("HTTPS" in warning for warning in warnings))

    def test_consent_required_blocks_plan(self) -> None:
        bundle = create_bundle(self.tmp / "bundles", "run")
        section = upload.build_upload_plan(
            bundle, upload_url="https://example.com/intake", consent=False
        )
        self.assertFalse(section["requested"])
        self.assertEqual(section["status"], "consent-required")
        self.assertEqual(section["network_performed"], False)
        self.assertFalse((bundle / "upload-plan.json").exists())

    def test_consent_writes_dry_run_plan_only(self) -> None:
        bundle = create_bundle(self.tmp / "bundles", "run")
        (bundle / "notes.txt").write_text("hello", encoding="utf-8")
        section = upload.build_upload_plan(
            bundle, upload_url="https://example.com/intake?token=abc", consent=True
        )
        self.assertTrue(section["requested"])
        self.assertEqual(section["mode"], "dry-run")
        self.assertEqual(section["status"], "planned-dry-run")
        self.assertEqual(section["network_performed"], False)
        self.assertIn("example.com", section["target"])
        self.assertNotIn("abc", section["target"])
        plan_path = bundle / "upload-plan.json"
        self.assertTrue(plan_path.is_file())
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_schema"], upload.UPLOAD_PLAN_SCHEMA)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["network_performed"], False)
        self.assertIn("notes.txt", {entry["relative_path"] for entry in plan["files"]})


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class ReportTests(TempDirTestCase):
    def test_summary_for_clean_and_crash_runs(self) -> None:
        clean = make_manifest(
            kind="run",
            bundle_dir=self.tmp / "clean",
            redactor=Redactor("none"),
            run={"exit": classify_returncode(0, system="Windows")},
        )
        self.assertEqual(report.summarize(clean)["verdict"], "clean")
        self.assertFalse(report.summarize(clean)["crash_suspected"])

        crash = make_manifest(
            kind="run",
            bundle_dir=self.tmp / "crash",
            redactor=Redactor("none"),
            run={"exit": classify_returncode(-11, system="Linux")},
        )
        summary = report.summarize(crash)
        self.assertEqual(summary["verdict"], "crash")
        self.assertTrue(summary["crash_suspected"])

        no_run = make_manifest(kind="collect", bundle_dir=self.tmp / "collect", redactor=Redactor("none"))
        self.assertEqual(report.summarize(no_run)["verdict"], "no-run-data")

    def test_markdown_report_renders_sections_and_os_name(self) -> None:
        bundle = self.make_bundle()
        redactor = Redactor("basic", username="alice", home=self.tmp / "home", hostname="host")
        manifest = make_manifest(
            kind="run",
            bundle_dir=bundle,
            redactor=redactor,
            system={
                "os": {"name": "Windows", "release": "11", "version": "10.0.26200"},
                "arch": "AMD64",
                "cpu": {"model": "Test CPU", "logical_cores": 8},
                "memory": {"total_bytes": 16 * 1024 ** 3},
                "python": {"version": "3.13.0"},
            },
            run={"exit": classify_returncode(0xC0000005, system="Windows"), "command": ["blender"]},
        )
        final = finalize_manifest(bundle, manifest, redactor)
        markdown = report.render_markdown(final)
        for heading in (
            "# Blender crash-monitor report",
            "## Verdict",
            "## Run",
            "## Exit status",
            "## Crash artifacts",
            "## Log tails",
            "## System, GPU, and Blender",
            "## Privacy and retention",
            "## Limitations",
        ):
            self.assertIn(heading, markdown)
        self.assertIn("**CRASH**", markdown)
        self.assertIn("Windows 11", markdown)
        self.assertNotIn("{'name':", markdown)

    def test_json_report_and_write(self) -> None:
        bundle, final, _ = self.make_manifest()
        payload = report.render_json_report(final)
        self.assertEqual(payload["report_schema"], "crashmon.report/1")
        self.assertEqual(payload["manifest"]["schema_version"], SCHEMA_VERSION)

        markdown_path = report.write_report(bundle, fmt="md")
        self.assertTrue(markdown_path.is_file())
        self.assertIn("Blender crash-monitor report", markdown_path.read_text(encoding="utf-8"))

        json_path = report.write_report(bundle, fmt="json", output=self.tmp / "report.json")
        written = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(written["report_schema"], "crashmon.report/1")


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


class CliTests(TempDirTestCase):
    def test_help_and_version(self) -> None:
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0)
        for command in ("run", "collect", "doctor", "report"):
            self.assertIn(command, help_result.stdout)

        version_result = self.run_cli("--version")
        self.assertEqual(version_result.returncode, 0)
        self.assertIn("crashmon", version_result.stdout)
        self.assertIn("prototype", version_result.stdout)

    def test_doctor_json_exits_zero_even_if_blender_missing(self) -> None:
        result = self.run_cli("doctor", "--skip-gpu", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["doctor_schema"], "crashmon.doctor/1")
        self.assertEqual(payload["network"], "disabled")
        names = {check["name"] for check in payload["checks"]}
        self.assertIn("python", names)
        self.assertIn("crash-artifact-source", names)
        self.assertIn("environment", names)

    def test_run_end_to_end_with_python_as_fake_blender(self) -> None:
        output_dir = self.tmp / "run-bundles"
        result = self.run_cli(
            "run",
            "--blender",
            sys.executable,
            "--output-dir",
            str(output_dir),
            "--skip-gpu",
            "--skip-probe",
            "--skip-artifacts",
            "--json",
            "--report",
            "--",
            "-c",
            "print('fake blender run')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["summary"]["exit_category"], "success")
        self.assertEqual(payload["validation_problems"], [])
        bundle = Path(payload["bundle_dir"])
        self.assertTrue((bundle / "manifest.json").is_file())
        self.assertTrue((bundle / "report.md").is_file())
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        stdout_entry = next(entry for entry in manifest["logs"] if entry["stream"] == "stdout")
        self.assertIn("fake blender run", stdout_entry["tail"])

    def test_collect_and_report_cli(self) -> None:
        log = self.tmp / "blender.log"
        log.write_text("collected output\n", encoding="utf-8")
        output_dir = self.tmp / "collect-bundles"
        result = self.run_cli(
            "collect",
            "--output-dir",
            str(output_dir),
            "--log",
            str(log),
            "--exit-code",
            "7",
            "--skip-gpu",
            "--skip-probe",
            "--skip-artifacts",
            "--json",
            "--report",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["summary"]["exit_category"], "error")
        bundle = Path(payload["bundle_dir"])
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["run"]["exit"]["category"], "error")
        self.assertEqual(manifest["logs"][0]["stream"], "imported")
        self.assertEqual(payload["validation_problems"], [])

        report_result = self.run_cli("report", str(bundle), "--format", "json")
        self.assertEqual(report_result.returncode, 0, report_result.stderr)
        rendered = json.loads(report_result.stdout)
        self.assertEqual(rendered["report_schema"], "crashmon.report/1")
        self.assertEqual(rendered["manifest"]["bundle"]["kind"], "collect")

    def test_upload_requires_consent(self) -> None:
        result = self.run_cli(
            "run",
            "--upload-url",
            "https://example.com/intake",
            "--blender",
            sys.executable,
            "--skip-gpu",
            "--skip-probe",
            "--skip-artifacts",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("--consent", result.stderr)


if __name__ == "__main__":
    unittest.main()
