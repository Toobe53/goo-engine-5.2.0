#!/usr/bin/env python3
"""Validate the production-build GitHub Actions workflow without third-party deps.

This repository's CI images must stay dependency-free, so this module ships a
small YAML subset parser instead of importing PyYAML. The parser understands the
constructs used by .github/workflows/production-build.yml:

  * block mappings and block sequences (including ``- key: value`` entries)
  * block scalars (``|`` and ``>`` with chomping/indent indicators)
  * flow sequences of plain/quoted scalars (``[opened, synchronize]``)
  * plain scalars, quoted scalars, booleans, null, integers, comments

Anything outside that subset is reported as an explicit error rather than
silently mis-parsed. Blank lines are dropped, and folded (``>``) block scalars
are joined with spaces; that is sufficient for structural validation but it is
not a byte-for-byte YAML round-trip.

Usage:
    python tools/ci/validate_workflow.py [path-to-workflow] [--json] [--quiet]

Exit codes:
    0  every required check passed
    1  the workflow parsed but failed one or more checks
    2  the workflow could not be parsed with the supported YAML subset

The script intentionally does not talk to GitHub. It validates the static
contract (triggers, label gate, matrix, timeouts, caching, build/update/upload
steps, honesty comments), not the behaviour of the hosted runners.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "production-build.yml"
)


class YamlSubsetError(ValueError):
    """The document uses YAML syntax outside the supported subset."""


# ---------------------------------------------------------------------------
# Minimal YAML subset parser
# ---------------------------------------------------------------------------

_BLOCK_SCALAR_RE = re.compile(r"^[|>](?:[+-]?\d?|\d[+-]?)$")
_QUOTED_KEY_RE = re.compile(r"""^('(?:[^']|'')*'|"(?:[^"\\]|\\.)*")\s*:(.*)$""")
_INTEGER_RE = re.compile(r"^-?\d+$")


def _indent_of(raw: str) -> int:
    return len(raw) - len(raw.lstrip(" "))


def _strip_comment(text: str) -> str:
    """Remove a trailing ``# comment`` that is outside quotes and flow brackets."""
    quote: Optional[str] = None
    depth = 0
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        elif char == "#" and depth == 0 and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
    return text.rstrip()


def _unquote_scalar(token: str, line_no: int) -> str:
    if token.startswith("'"):
        if len(token) < 2 or not token.endswith("'"):
            raise YamlSubsetError(f"line {line_no}: unterminated single-quoted scalar")
        return token[1:-1].replace("''", "'")
    if token.startswith('"'):
        if len(token) < 2 or not token.endswith('"'):
            raise YamlSubsetError(f"line {line_no}: unterminated double-quoted scalar")
        body = token[1:-1]
        try:
            return json.loads('"' + body + '"')
        except ValueError:
            # Fall back to a conservative unescape for YAML-only escapes.
            return body.replace('\\"', '"').replace("\\\\", "\\")
    return token


def _parse_scalar(token: str, line_no: int) -> Any:
    token = token.strip()
    if not token:
        raise YamlSubsetError(f"line {line_no}: empty scalar")
    if token[0] in ("'", '"'):
        return _unquote_scalar(token, line_no)
    if token[0] in "[{":
        raise YamlSubsetError(f"line {line_no}: nested flow collections are not supported")
    if token[0] in "&*!":
        raise YamlSubsetError(f"line {line_no}: anchors, aliases and tags are not supported")
    lowered = token.lower()
    if lowered in ("null", "~"):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _INTEGER_RE.match(token):
        return int(token)
    return token


def _split_flow_items(body: str, line_no: int) -> List[str]:
    items: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    depth = 0
    for char in body:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            current.append(char)
        elif char in "[{":
            depth += 1
            current.append(char)
        elif char in "]}":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    if quote is not None:
        raise YamlSubsetError(f"line {line_no}: unterminated quote in flow sequence")
    tail = "".join(current)
    if tail.strip():
        items.append(tail)
    return [item.strip() for item in items if item.strip()]


def _parse_inline(text: str, line_no: int) -> Any:
    text = text.strip()
    if text.startswith("["):
        if not text.endswith("]"):
            raise YamlSubsetError(f"line {line_no}: unterminated flow sequence")
        return [_parse_scalar(item, line_no) for item in _split_flow_items(text[1:-1], line_no)]
    return _parse_scalar(text, line_no)


def _split_key(text: str, line_no: int) -> Tuple[str, str]:
    match = _QUOTED_KEY_RE.match(text)
    if match:
        return _unquote_scalar(match.group(1), line_no), match.group(2)
    quote: Optional[str] = None
    depth = 0
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        elif char == ":" and depth == 0:
            if text[index + 1 : index + 2] in ("", " "):
                key = text[:index].strip()
                if not key:
                    raise YamlSubsetError(f"line {line_no}: empty mapping key")
                return key, text[index + 1 :]
    raise YamlSubsetError(f"line {line_no}: expected 'key: value', got {text!r}")


def _looks_like_key(text: str, line_no: int) -> bool:
    try:
        _split_key(text, line_no)
    except YamlSubsetError:
        return False
    return True


class _Parser:
    def __init__(self, text: str) -> None:
        # Blank lines carry no structural meaning for this validator and are
        # dropped so block-scalar end detection stays simple.
        self.raw: List[Tuple[int, str]] = [
            (line_no, line)
            for line_no, line in enumerate(text.splitlines(), start=1)
            if line.strip()
        ]
        self.index = 0

    def parse(self) -> Any:
        peeked = self._peek()
        if peeked is None:
            return {}
        indent, _text, _index, line_no = peeked
        if indent != 0:
            raise YamlSubsetError(f"line {line_no}: document must start at column 0")
        value = self._parse_block(indent)
        rest = self._peek()
        if rest is not None:
            raise YamlSubsetError(f"line {rest[3]}: unexpected trailing document content")
        return value

    def _peek(self) -> Optional[Tuple[int, str, int, int]]:
        index = self.index
        while index < len(self.raw):
            line_no, raw = self.raw[index]
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                index += 1
                continue
            if raw.startswith("\t"):
                raise YamlSubsetError(f"line {line_no}: tab indentation is not supported")
            return _indent_of(raw), _strip_comment(stripped), index, line_no
        return None

    def _parse_block(self, indent: int) -> Any:
        peeked = self._peek()
        if peeked is None:
            return None
        found_indent, text, _index, line_no = peeked
        if found_indent != indent:
            raise YamlSubsetError(
                f"line {line_no}: expected indentation {indent}, found {found_indent}"
            )
        if text == "-" or text.startswith("- "):
            return self._parse_sequence(indent)
        return self._parse_mapping(indent)

    def _parse_sequence(self, indent: int) -> List[Any]:
        items: List[Any] = []
        while True:
            peeked = self._peek()
            if peeked is None:
                break
            found_indent, text, index, line_no = peeked
            if found_indent < indent:
                break
            if found_indent > indent:
                raise YamlSubsetError(
                    f"line {line_no}: unexpected indentation in sequence (expected {indent})"
                )
            if not (text == "-" or text.startswith("- ")):
                break
            rest = text[1:].strip()
            self.index = index + 1
            if rest == "":
                child = self._peek()
                if child is not None and child[0] > indent:
                    items.append(self._parse_block(child[0]))
                else:
                    items.append(None)
                continue
            if _looks_like_key(rest, line_no):
                key, value_text = _split_key(rest, line_no)
                map_indent = indent + 2
                item: dict = {key: self._consume_value(value_text.strip(), map_indent, line_no)}
                self._parse_mapping_entries(item, map_indent)
                items.append(item)
            else:
                items.append(_parse_inline(rest, line_no))
        return items

    def _parse_mapping(self, indent: int) -> dict:
        result: dict = {}
        self._parse_mapping_entries(result, indent)
        return result

    def _parse_mapping_entries(self, target: dict, indent: int) -> None:
        while True:
            peeked = self._peek()
            if peeked is None:
                return
            found_indent, text, index, line_no = peeked
            if found_indent < indent:
                return
            if found_indent > indent:
                raise YamlSubsetError(
                    f"line {line_no}: unexpected indentation in mapping (expected {indent})"
                )
            if text == "-" or text.startswith("- "):
                raise YamlSubsetError(f"line {line_no}: sequence item where a key was expected")
            key, value_text = _split_key(text, line_no)
            self.index = index + 1
            target[key] = self._consume_value(value_text.strip(), indent, line_no)

    def _consume_value(self, rest: str, indent: int, line_no: int) -> Any:
        if not rest:
            child = self._peek()
            if child is not None and child[0] > indent:
                return self._parse_block(child[0])
            return None
        if _BLOCK_SCALAR_RE.match(rest):
            return self._consume_block_scalar(indent, line_no, rest)
        return _parse_inline(rest, line_no)

    def _consume_block_scalar(self, parent_indent: int, line_no: int, style: str) -> str:
        collected: List[str] = []
        cursor = self.index
        while cursor < len(self.raw):
            raw = self.raw[cursor][1]
            if _indent_of(raw) <= parent_indent:
                break
            collected.append(raw)
            cursor += 1
        if not collected:
            self.index = cursor
            return ""
        min_indent = min(_indent_of(raw) for raw in collected if raw.strip())
        lines = [raw[min_indent:] if raw.strip() else "" for raw in collected]
        self.index = cursor
        text = "\n".join(lines)
        # Honour YAML chomping so the result matches yaml.safe_load for the
        # constructs used here (blank lines are dropped by the parser, so `+`
        # cannot preserve multiple trailing newlines exactly).
        strip_chomp = "-" in style
        if style.startswith(">"):
            # Folded scalars: for structural validation, joining non-empty lines
            # with spaces is enough (we only look for substrings in `if:` gates).
            folded = " ".join(part.strip() for part in text.splitlines() if part.strip())
            return folded if strip_chomp else folded + "\n"
        return text if strip_chomp else text + "\n"


def parse_workflow_yaml(text: str) -> Any:
    """Parse the workflow with the dependency-free YAML subset parser."""
    return _Parser(text).parse()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _step_run(step: dict) -> str:
    run = step.get("run")
    return run if isinstance(run, str) else ""


def _step_if(step: dict) -> str:
    condition = step.get("if")
    return condition if isinstance(condition, str) else ""


def _step_with(step: dict) -> dict:
    return _as_dict(step.get("with"))


def _step_uses(step: dict) -> str:
    uses = step.get("uses")
    return uses if isinstance(uses, str) else ""


class _Report:
    def __init__(self) -> None:
        self.checks: List[dict] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})

    @property
    def failures(self) -> List[dict]:
        return [check for check in self.checks if not check["ok"]]

    @property
    def ok(self) -> bool:
        return not self.failures


def _find_build_job(jobs: dict) -> Tuple[Optional[str], Optional[dict]]:
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        strategy = _as_dict(job.get("strategy"))
        if isinstance(strategy.get("matrix"), dict):
            return job_name, job
    return None, None


def validate_workflow_data(data: Any, raw_text: str) -> _Report:
    report = _Report()

    root = _as_dict(data)
    report.check("workflow root is a mapping", isinstance(data, dict))
    report.check("workflow has a non-empty name", isinstance(root.get("name"), str) and bool(root.get("name")))

    # --- triggers -----------------------------------------------------------
    on_block = root.get("on")
    report.check("workflow declares an 'on:' trigger block", isinstance(on_block, dict))
    triggers = _as_dict(on_block)
    for trigger in ("workflow_dispatch", "push", "pull_request"):
        report.check(f"trigger '{trigger}' is configured", trigger in triggers)

    push_block = _as_dict(triggers.get("push"))
    push_branches = _as_list(push_block.get("branches"))
    report.check(
        "push builds production/** branches",
        any(isinstance(branch, str) and branch.startswith("production/") for branch in push_branches),
        f"branches={push_branches!r}",
    )

    pull_request_block = _as_dict(triggers.get("pull_request"))
    pr_types = [item for item in _as_list(pull_request_block.get("types")) if isinstance(item, str)]
    report.check(
        "pull_request listens for opened/synchronize/reopened/labeled",
        {"opened", "synchronize", "reopened", "labeled"}.issubset(set(pr_types)),
        f"types={pr_types!r}",
    )

    # --- permissions / concurrency -----------------------------------------
    permissions = _as_dict(root.get("permissions"))
    report.check(
        "permissions are read-only (contents: read)",
        permissions.get("contents") == "read" and "write" not in json.dumps(permissions),
        f"permissions={permissions!r}",
    )
    report.check("workflow defines a concurrency group", "concurrency" in root)

    # --- job, gate, timeout, matrix ----------------------------------------
    jobs = _as_dict(root.get("jobs"))
    job_name, job = _find_build_job(jobs)
    report.check("exactly one build matrix job exists", isinstance(job, dict))
    if job is None:
        return report
    job = _as_dict(job)

    job_gate = _step_if(job)  # same accessor shape works for job-level 'if'
    report.check(
        "PR builds are gated on the 'ci:build' label",
        "pull_request" in job_gate and "ci:build" in job_gate and "labels" in job_gate,
        f"if={job_gate!r}",
    )

    strategy = _as_dict(job.get("strategy"))
    report.check("matrix fail-fast is false", strategy.get("fail-fast") is False)

    timeout = job.get("timeout-minutes")
    report.check(
        "job timeout-minutes is at least 120",
        isinstance(timeout, int) and timeout >= 120,
        f"timeout-minutes={timeout!r}",
    )

    runs_on = job.get("runs-on")
    report.check(
        "job runs on a matrix-provided runner label",
        isinstance(runs_on, str) and "matrix." in runs_on,
        f"runs-on={runs_on!r}",
    )

    matrix = _as_dict(strategy.get("matrix"))
    include = _as_list(matrix.get("include"))
    report.check("matrix uses two explicit include entries", len(include) == 2, f"count={len(include)}")

    entries = [entry for entry in include if isinstance(entry, dict)]
    names = [entry.get("name") for entry in entries]
    report.check(
        "matrix covers windows-x64 and linux-x64 (macOS out of scope)",
        set(names) == {"windows-x64", "linux-x64"},
        f"names={names!r}",
    )

    expected_os = {
        "windows-x64": ("windows", "windows_x64"),
        "linux-x64": ("ubuntu", "linux_x64"),
    }
    for entry in entries:
        name = entry.get("name")
        for field in ("os", "libdir", "build_dir", "out_dir", "artifact"):
            report.check(f"matrix entry {name!r} defines {field}", entry.get(field) not in (None, ""))
        os_label = str(entry.get("os", ""))
        libdir = str(entry.get("libdir", ""))
        expected_prefix, expected_libdir = expected_os.get(str(name), ("", ""))
        report.check(
            f"matrix entry {name!r} uses a {expected_prefix or 'supported'} runner image",
            bool(expected_prefix) and os_label.startswith(expected_prefix),
            f"os={os_label!r}",
        )
        report.check(
            f"matrix entry {name!r} uses the expected precompiled library dir",
            libdir == expected_libdir,
            f"libdir={libdir!r}",
        )
        build_dir = str(entry.get("build_dir", ""))
        out_dir = str(entry.get("out_dir", ""))
        report.check(
            f"matrix entry {name!r} keeps build output inside a relative build dir",
            bool(build_dir)
            and bool(out_dir)
            and out_dir.startswith(build_dir)
            and ".." not in out_dir
            and not Path(out_dir).is_absolute(),
            f"build_dir={build_dir!r} out_dir={out_dir!r}",
        )
    report.check(
        "matrix artifact names are unique",
        len({entry.get("artifact") for entry in entries}) == len(entries),
    )

    # --- steps --------------------------------------------------------------
    steps = [step for step in _as_list(job.get("steps")) if isinstance(step, dict)]
    report.check("job defines steps", bool(steps))
    uses_list = [_step_uses(step) for step in steps]
    run_list = [_step_run(step) for step in steps]

    report.check(
        "checks out the repository with actions/checkout",
        any(uses.startswith("actions/checkout@") for uses in uses_list),
    )
    report.check(
        "sets up Python for build scripts",
        any(uses.startswith("actions/setup-python@") for uses in uses_list),
    )

    # Library update must be non-interactive on all platforms.
    update_runs = [run for run in run_list if "make_update.py" in run]
    report.check("fetches precompiled libraries through make_update.py", bool(update_runs))
    report.check(
        "library update skips the authoritative-repo pull (--no-blender)",
        any("--no-blender" in run for run in update_runs),
        "make_update.py must not try to pull the branch under a CI checkout",
    )
    report.check(
        "workflow never invokes the interactive 'make.bat update'",
        "make.bat update" not in raw_text,
    )

    # Caches.
    cache_steps = [step for step in steps if _step_uses(step).startswith("actions/cache@")]
    cache_paths = [str(_step_with(step).get("path", "")) for step in cache_steps]
    cache_keys = [str(_step_with(step).get("key", "")) for step in cache_steps]
    report.check("declares at least two actions/cache steps", len(cache_steps) >= 2)
    report.check(
        "caches precompiled libraries (lib/<platform>)",
        any("lib/" in path for path in cache_paths),
        f"paths={cache_paths!r}",
    )
    report.check(
        "library cache key is content/commit derived and epoch-tagged",
        any("libs-" in key and "CI_CACHE_EPOCH" in key for key in cache_keys),
        f"keys={cache_keys!r}",
    )
    report.check(
        "caches CMake configure state (CMakeCache.txt)",
        any("CMakeCache.txt" in path for path in cache_paths),
    )
    report.check(
        "caches ccache for Unix builds",
        any("CCACHE_DIR" in path or "ccache" in path.lower() for path in cache_paths),
    )

    # Release builds.
    unix_builds = [step for step in steps if "make release ninja" in _step_run(step)]
    windows_builds = [step for step in steps if "make.bat release ninja" in _step_run(step)]
    report.check("Unix release build uses 'make release ninja'", bool(unix_builds))
    report.check("Windows release build uses 'make.bat release ninja'", bool(windows_builds))
    report.check(
        "Unix release build passes an explicit BUILD_DIR",
        any("BUILD_DIR=" in _step_run(step) for step in unix_builds),
    )
    report.check(
        "Windows release build passes an explicit builddir",
        any("builddir" in _step_run(step) for step in windows_builds),
    )
    report.check(
        "no macOS release build step remains in scope",
        not any("macOS" in _step_if(step) for step in unix_builds),
        "macOS builds must not be part of the Windows/Linux delivery",
    )
    build_steps = unix_builds + windows_builds
    report.check(
        "release build steps are not allowed to fail silently (continue-on-error)",
        all(step.get("continue-on-error") not in (True, "true") for step in build_steps),
    )
    report.check(
        "release build steps are OS-gated",
        all(("runner.os" in _step_if(step) or "matrix.name" in _step_if(step)) for step in build_steps),
    )

    # Artifacts.
    upload_steps = [step for step in steps if _step_uses(step).startswith("actions/upload-artifact@")]
    report.check("uploads both logs and build output artifacts", len(upload_steps) >= 2)
    log_uploads = [
        step
        for step in upload_steps
        if "always()" in _step_if(step) and "ci-logs" in str(_step_with(step).get("path", ""))
    ]
    report.check(
        "uploads logs even when the build fails (if: always())",
        bool(log_uploads),
    )
    output_uploads = [
        step
        for step in upload_steps
        if "success()" in _step_if(step) and "bin/" in str(_step_with(step).get("path", ""))
    ]
    report.check(
        "uploads build output only after a successful build",
        bool(output_uploads),
    )
    report.check(
        "output artifact names are matrix-specific",
        any("matrix.artifact" in str(_step_with(step).get("name", "")) for step in output_uploads),
    )

    # --- honesty / scope comments ------------------------------------------
    report.check(
        "documents that hosted runners have no GPU",
        re.search(r"\bno\s+gpu\b", raw_text, re.IGNORECASE) is not None,
    )
    report.check(
        "documents that EEVEE/render regressions are out of scope",
        "EEVEE" in raw_text,
    )
    report.check(
        "points GPU regression coverage at the self-hosted driver matrix",
        "self-hosted" in raw_text.lower() and "driver matrix" in raw_text.lower(),
    )
    report.check(
        "documents macOS is out of scope and hosted CI is not the acceptance gate",
        "macOS is out of scope" in raw_text and "not the acceptance gate" in raw_text,
    )

    return report


def validate_workflow(path: Path) -> Tuple[_Report, Optional[dict]]:
    text = path.read_text(encoding="utf-8")
    data = parse_workflow_yaml(text)
    return validate_workflow_data(data, text), {"data": data}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "workflow",
        nargs="?",
        default=str(DEFAULT_WORKFLOW),
        help="workflow YAML to validate (default: .github/workflows/production-build.yml)",
    )
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    parser.add_argument("--quiet", action="store_true", help="only print failures and the summary")
    args = parser.parse_args(argv)

    path = Path(args.workflow)
    if not path.is_file():
        print(f"error: workflow not found: {path}", file=sys.stderr)
        return 2

    try:
        report, parsed = validate_workflow(path)
    except YamlSubsetError as exc:
        print(f"error: cannot parse {path} with the YAML subset parser: {exc}", file=sys.stderr)
        print(
            "hint: this validator supports the constructs listed in its module docstring; "
            "update the parser if the workflow intentionally uses a new construct.",
            file=sys.stderr,
        )
        return 2

    payload = {
        "workflow": str(path),
        "ok": report.ok,
        "checks": report.checks,
        "failed": len(report.failures),
        "passed": len(report.checks) - len(report.failures),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Validating workflow: {path}")
        for check in report.checks:
            if args.quiet and check["ok"]:
                continue
            marker = "ok  " if check["ok"] else "FAIL"
            line = f"[{marker}] {check['name']}"
            if check["detail"] and not check["ok"]:
                line += f" ({check['detail']})"
            print(line)
        print()
        print(f"{payload['passed']} checks passed, {payload['failed']} failed")

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
