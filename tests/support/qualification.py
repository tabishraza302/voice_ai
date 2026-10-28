"""Pure verification and qualification policy helpers.

Implements the contract specified in verification-plan.md Section 6.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class GateCounts:
    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    xfailed: int
    xpassed: int


def require_gate(counts: GateCounts) -> None:
    if counts.collected == 0:
        raise ValueError("required gate collected zero tests")
    if counts.passed == 0:
        raise ValueError("required gate passed zero tests")
    if any(
        (
            counts.failed,
            counts.errors,
            counts.skipped,
            counts.xfailed,
            counts.xpassed,
        )
    ):
        raise ValueError("required gate contains non-pass outcomes")


def prerequisite_action(*, available: bool, required: bool) -> str:
    if available:
        return "run"
    if required:
        raise ValueError("required qualification prerequisite unavailable")
    return "skip"


def redact_secrets(text: str) -> str:
    """Redact passwords and tokens from URLs and log text."""
    if not text:
        return ""
    # Redact postgres and http/https credentials
    sanitized = re.sub(
        r"([a-zA-Z0-9+._-]+://[^:]+:)([^@]+)(@)",
        r"\1***\3",
        text,
    )
    # Redact common token patterns in query params or headers
    sanitized = re.sub(
        r"(token|secret|password|key)=([a-zA-Z0-9_\-\.]+)",
        r"\1=***",
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized


def parse_junit_xml(path: Path | str) -> GateCounts:
    """Parse pytest JUnit XML into strict GateCounts."""
    report_path = Path(path)
    if not report_path.is_file():
        raise FileNotFoundError(f"required report missing: {report_path}")

    try:
        tree = ET.parse(report_path)
    except ET.ParseError as err:
        raise ValueError(f"malformed JUnit XML report {report_path}: {err}") from err

    root = tree.getroot()

    collected = 0
    passed = 0
    failed = 0
    errors = 0
    skipped = 0
    xfailed = 0
    xpassed = 0

    testcases = root.findall(".//testcase")
    if not testcases:
        # Check if root or testsuite has attributes indicating empty or errored run
        total_tests = int(root.attrib.get("tests", 0))
        total_errors = int(root.attrib.get("errors", 0))
        total_failures = int(root.attrib.get("failures", 0))
        total_skipped = int(root.attrib.get("skipped", 0))
        collected = total_tests
        errors = total_errors
        failed = total_failures
        skipped = total_skipped
        passed = max(0, collected - (errors + failed + skipped))
        return GateCounts(
            collected=collected,
            passed=passed,
            failed=failed,
            errors=errors,
            skipped=skipped,
            xfailed=0,
            xpassed=0,
        )

    for tc in testcases:
        collected += 1
        fail_el = tc.find("failure")
        err_el = tc.find("error")
        skip_el = tc.find("skipped")

        if fail_el is not None:
            failed += 1
        elif err_el is not None:
            errors += 1
        elif skip_el is not None:
            msg = (skip_el.attrib.get("message") or "").lower()
            skip_type = (skip_el.attrib.get("type") or "").lower()
            if "xfail" in msg or "xfail" in skip_type:
                xfailed += 1
            elif "xpass" in msg or "xpass" in skip_type:
                xpassed += 1
            else:
                skipped += 1
        else:
            passed += 1

    return GateCounts(
        collected=collected,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        xfailed=xfailed,
        xpassed=xpassed,
    )


def parse_bun_output(output: str) -> GateCounts:
    """Parse captured Bun test runner output into strict GateCounts."""
    if not output or not output.strip():
        raise ValueError("missing Bun test runner output")

    # Strip ANSI color escapes and match anchored summary lines
    clean_output = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output)

    pass_match = re.search(r"^\s*(\d+)\s+pass\b", clean_output, re.MULTILINE)
    fail_match = re.search(r"^\s*(\d+)\s+fail\b", clean_output, re.MULTILINE)
    skip_match = re.search(r"^\s*(\d+)\s+skip\b", clean_output, re.MULTILINE)
    todo_match = re.search(r"^\s*(\d+)\s+todo\b", clean_output, re.MULTILINE)
    error_match = re.search(r"^\s*(\d+)\s+error\b", clean_output, re.MULTILINE)

    if todo_match and int(todo_match.group(1)) > 0:
        raise ValueError(f"required gate contains todo tests: {todo_match.group(1)} todo")

    if not pass_match and not fail_match:
        ran_match = re.search(r"^\s*Ran\s+(\d+)\s+tests\b", clean_output, re.MULTILINE)
        if not ran_match:
            raise ValueError(f"unparseable Bun test report: {clean_output[-200:]}")
        raise ValueError(f"unparseable Bun test summary lines: {clean_output[-200:]}")
    passed = int(pass_match.group(1)) if pass_match else 0
    failed = int(fail_match.group(1)) if fail_match else 0
    skipped = int(skip_match.group(1)) if skip_match else 0
    errors = int(error_match.group(1)) if error_match else 0

    collected = passed + failed + skipped + errors

    return GateCounts(
        collected=collected,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        xfailed=0,
        xpassed=0,
    )


def parse_playwright_json(data_or_path: dict[str, Any] | str | Path) -> GateCounts:
    """Parse Playwright JSON reporter output into strict GateCounts."""
    if isinstance(data_or_path, (str, Path)):
        p = Path(data_or_path)
        if not p.is_file():
            raise FileNotFoundError(f"Playwright report file missing: {p}")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise ValueError(f"malformed Playwright JSON report: {err}") from err
    else:
        raw = data_or_path

    stats = raw.get("stats")
    if not stats:
        # If no stats block, try walking suites
        suites = raw.get("suites")
        if not suites:
            raise ValueError("Playwright report missing stats and suites")
        collected = 0
        passed = 0
        failed = 0
        skipped = 0

        def walk(suite: dict[str, Any]) -> None:
            nonlocal collected, passed, failed, skipped
            for spec in suite.get("specs", []):
                for test in spec.get("tests", []):
                    collected += 1
                    status = test.get("status")
                    if status == "expected":
                        passed += 1
                    elif status == "skipped":
                        skipped += 1
                    else:
                        failed += 1
            for child in suite.get("suites", []):
                walk(child)

        for s in suites:
            walk(s)

        return GateCounts(
            collected=collected,
            passed=passed,
            failed=failed,
            errors=0,
            skipped=skipped,
            xfailed=0,
            xpassed=0,
        )

    expected = stats.get("expected", 0)
    unexpected = stats.get("unexpected", 0)
    flaky = stats.get("flaky", 0)
    skipped = stats.get("skipped", 0)

    # In strict verification: flaky is treated as failed (no retries tolerated)
    failed = unexpected + flaky
    collected = expected + failed + skipped

    return GateCounts(
        collected=collected,
        passed=expected,
        failed=failed,
        errors=0,
        skipped=skipped,
        xfailed=0,
        xpassed=0,
    )
