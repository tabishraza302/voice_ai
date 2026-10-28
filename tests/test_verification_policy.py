"""Unit tests for verification and qualification policies.

Implements the contract specified in verification-plan.md Section 6.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from tests.support.qualification import (
    GateCounts,
    parse_bun_output,
    parse_junit_xml,
    parse_playwright_json,
    prerequisite_action,
    redact_secrets,
    require_gate,
)


def test_optional_developer_prerequisite_skips() -> None:
    assert prerequisite_action(available=False, required=False) == "skip"


def test_optional_developer_prerequisite_runs_when_available() -> None:
    assert prerequisite_action(available=True, required=False) == "run"


def test_required_prerequisite_missing_is_failure() -> None:
    with pytest.raises(ValueError, match="required qualification prerequisite unavailable"):
        prerequisite_action(available=False, required=True)


def test_required_prerequisite_runs_when_available() -> None:
    assert prerequisite_action(available=True, required=True) == "run"


@pytest.mark.parametrize("field", ["skipped", "xfailed", "xpassed", "failed", "errors"])
def test_required_skip_or_xfail_fails_gate(field: str) -> None:
    values = dict(
        collected=2,
        passed=1,
        failed=0,
        errors=0,
        skipped=0,
        xfailed=0,
        xpassed=0,
    )
    values[field] = 1
    with pytest.raises(ValueError, match="required gate contains non-pass outcomes"):
        require_gate(GateCounts(**values))


def test_zero_collection_fails_gate() -> None:
    with pytest.raises(ValueError, match="required gate collected zero tests"):
        require_gate(GateCounts(0, 0, 0, 0, 0, 0, 0))


def test_zero_passed_fails_gate() -> None:
    with pytest.raises(ValueError, match="required gate passed zero tests"):
        require_gate(GateCounts(1, 0, 1, 0, 0, 0, 0))


def test_all_passed_gate_is_accepted() -> None:
    require_gate(GateCounts(3, 3, 0, 0, 0, 0, 0))


def test_missing_report_fails_gate(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent-report.xml"
    with pytest.raises(FileNotFoundError, match="required report missing"):
        parse_junit_xml(missing)


def test_parse_junit_xml_clean_pass(tmp_path: Path) -> None:
    report = tmp_path / "clean.xml"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="2" errors="0" failures="0" skipped="0">
    <testcase classname="tests.test_a" name="test_one" time="0.001" />
    <testcase classname="tests.test_a" name="test_two" time="0.002" />
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )
    counts = parse_junit_xml(report)
    assert counts == GateCounts(
        collected=2, passed=2, failed=0, errors=0, skipped=0, xfailed=0, xpassed=0
    )
    require_gate(counts)


def test_parse_junit_xml_with_skip_and_xfail(tmp_path: Path) -> None:
    report = tmp_path / "mixed.xml"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="3" errors="0" failures="0" skipped="2">
  <testcase classname="tests.test_a" name="test_ok" />
  <testcase classname="tests.test_a" name="test_skip">
    <skipped message="skipped by user" type="pytest.skip" />
  </testcase>
  <testcase classname="tests.test_a" name="test_xfail">
    <skipped message="XFAIL expected" type="pytest.xfail" />
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )
    counts = parse_junit_xml(report)
    assert counts.collected == 3
    assert counts.passed == 1
    assert counts.skipped == 1
    assert counts.xfailed == 1
    with pytest.raises(ValueError, match="non-pass outcomes"):
        require_gate(counts)


def test_parse_bun_output_clean_pass() -> None:
    bun_output = """
bun test v1.3.14 (macOS arm64)

src/lib/a.test.mjs:
✓ a > returns true [0.12ms]

src/lib/b.test.mjs:
✓ b > handles values [0.34ms]

 2 pass
 0 fail
 2 expect() calls
Ran 2 tests across 2 files. [4.00ms]
"""
    counts = parse_bun_output(bun_output)
    assert counts.collected == 2
    assert counts.passed == 2
    assert counts.failed == 0
    assert counts.skipped == 0
    require_gate(counts)


def test_parse_bun_output_with_skips_fails_gate() -> None:
    bun_output = """
 2 pass
 1 skip
 0 fail
Ran 3 tests across 2 files.
"""
    counts = parse_bun_output(bun_output)
    assert counts.collected == 3
    assert counts.skipped == 1
    with pytest.raises(ValueError, match="non-pass outcomes"):
        require_gate(counts)



def test_parse_bun_output_with_todo_fails_closed() -> None:
    """Bun test runner output with todo tests must fail closed."""
    bun_output = """
 2 pass
 1 todo
 0 fail
Ran 3 tests across 2 files.
"""
    with pytest.raises(ValueError, match="contains todo tests"):
        parse_bun_output(bun_output)


def test_parse_bun_output_from_combined_stderr() -> None:
    """Bun reports can appear in stderr; combined output must parse accurately."""
    combined = """
(stdout)
$ bun test
(stderr)
 5 pass
 0 fail
Ran 5 tests across 3 files. [12.00ms]
"""
    counts = parse_bun_output(combined)
    assert counts.collected == 5
    assert counts.passed == 5
    require_gate(counts)

def test_parse_playwright_json_clean_pass(tmp_path: Path) -> None:
    data = {
        "stats": {
            "startTime": "2026-10-28T00:00:00.000Z",
            "duration": 1200,
            "expected": 4,
            "unexpected": 0,
            "flaky": 0,
            "skipped": 0,
        }
    }
    p = tmp_path / "playwright.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    counts = parse_playwright_json(p)
    assert counts == GateCounts(4, 4, 0, 0, 0, 0, 0)
    require_gate(counts)


def test_parse_playwright_json_flaky_or_skipped_fails_gate(tmp_path: Path) -> None:
    data = {
        "stats": {
            "startTime": "2026-10-28T00:00:00.000Z",
            "duration": 1200,
            "expected": 3,
            "unexpected": 0,
            "flaky": 1,
            "skipped": 1,
        }
    }
    p = tmp_path / "playwright.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    counts = parse_playwright_json(p)
    assert counts.failed == 1  # flaky treated as failed
    assert counts.skipped == 1
    with pytest.raises(ValueError, match="non-pass outcomes"):
        require_gate(counts)


def test_redact_secrets() -> None:
    sensitive = "Connecting to postgresql+asyncpg://myuser:secret123@127.0.0.1:5432/platform_db?token=abcdef"
    sanitized = redact_secrets(sensitive)
    assert "secret123" not in sanitized
    assert "myuser:***@" in sanitized
    assert "token=***" in sanitized


def test_redact_secrets_preserves_json_with_url_and_email() -> None:
    json_text = '{"baseURL": "http://127.0.0.1:58714", "user": "admin-1793188841897@example.com"}'
    sanitized = redact_secrets(json_text)
    assert sanitized == json_text
    with_creds = '{"db": "postgresql://myuser:supersecret@127.0.0.1:5432/db"}'
    masked = redact_secrets(with_creds)
    assert "supersecret" not in masked
    assert "postgresql://myuser:***@127.0.0.1:5432/db" in masked
