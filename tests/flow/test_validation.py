"""Tests for conversation flow authoring and executable validation.

Per docs/first-release/flow-contract.md sections 2, 3:
- Tests all fixtures against expected authoring_valid, executable_valid, and error_codes.
- Tests semantic checks, size bounds, DAG acyclicity, and sorting of diagnostics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.flow.schemas import FlowDiagnostic, FlowLimits
from bot.flow.validation import (
    normalize_executable,
    validate_authoring,
    validate_executable,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "flow"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


class TestFixtureValidation:
    """Validate all checked contract fixtures against their declared expectations."""

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "authoring-empty.json",
            "authoring-missing-target.json",
            "invalid-authored-cycle.json",
            "invalid-dangling-target.json",
            "invalid-impossible-prerequisite.json",
            "invalid-operator-field-type.json",
            "invalid-operator.json",
            "invalid-unreachable-step.json",
            "valid-credit-card-en-hi.json",
            "valid-first-match-overlap.json",
            "valid-loan-en-hi.json",
        ],
    )
    def test_fixture_expectations(self, fixture_name: str) -> None:
        data = _load_fixture(fixture_name)
        expected = data["expected"]
        flow = data["flow"]

        # Authoring structure check
        auth_diagnostics = validate_authoring(flow)
        auth_has_errors = any(d.severity == "error" for d in auth_diagnostics)
        assert (not auth_has_errors) == expected["authoring_valid"], (
            f"Fixture {fixture_name} authoring_valid mismatch: "
            f"got {not auth_has_errors}, expected {expected['authoring_valid']}. "
            f"Diagnostics: {auth_diagnostics}"
        )

        # Executable validation check
        exec_diagnostics = validate_executable(flow)
        exec_errors = [d for d in exec_diagnostics if d.severity == "error"]
        exec_has_errors = len(exec_errors) > 0
        assert (not exec_has_errors) == expected["executable_valid"], (
            f"Fixture {fixture_name} executable_valid mismatch: "
            f"got {not exec_has_errors}, expected {expected['executable_valid']}. "
            f"Errors: {exec_errors}"
        )

        # Check declared expected error codes
        actual_error_codes = set(d.code for d in exec_errors)
        for exp_code in expected.get("error_codes", []):
            assert exp_code in actual_error_codes, (
                f"Fixture {fixture_name} missing expected error code {exp_code}. "
                f"Actual error codes: {actual_error_codes}"
            )

        # Check declared warnings if any
        if data.get("warnings"):
            warn_codes = set(d.code for d in exec_diagnostics if d.severity == "warning")
            for exp_warn in data["warnings"]:
                assert exp_warn in warn_codes, (
                    f"Fixture {fixture_name} missing expected warning {exp_warn}. "
                    f"Actual warnings: {warn_codes}"
                )


class TestSemanticValidationRules:
    """Targeted tests for specific semantic and structural validation rules."""

    def test_blank_title_and_instruction(self) -> None:
        flow = {
            "schema_version": 1,
            "entry_step_id": "step1",
            "exit_step_id": "step2",
            "steps": [
                {
                    "id": "step1",
                    "title": "   ",
                    "type": "collect",
                    "instruction": "",
                    "required": True,
                    "priority": 10,
                    "prerequisites": [],
                    "captures": [{"field": "f1", "kind": "string", "max_length": 64}],
                    "transitions": [{"when": {"operator": "is_present", "field": "f1"}, "to": "step2"}],
                },
                {
                    "id": "step2",
                    "title": "End",
                    "type": "end",
                    "instruction": "Goodbye",
                    "required": False,
                    "priority": 100,
                    "prerequisites": [],
                    "captures": [],
                    "transitions": [],
                    "messages": [{"id": "m1", "en": "Bye", "hi": "नमस्ते"}],
                },
            ],
        }
        diagnostics = validate_executable(flow)
        codes = [d.code for d in diagnostics if d.severity == "error"]
        assert "INCOMPLETE_STEP" in codes

    def test_duplicate_step_id(self) -> None:
        flow = {
            "schema_version": 1,
            "entry_step_id": "step1",
            "exit_step_id": "end",
            "steps": [
                {
                    "id": "step1",
                    "title": "Step 1",
                    "type": "collect",
                    "instruction": "Do 1",
                    "required": True,
                    "priority": 10,
                    "prerequisites": [],
                    "captures": [{"field": "f1", "kind": "string"}],
                    "transitions": [{"when": {"operator": "is_present", "field": "f1"}, "to": "end"}],
                },
                {
                    "id": "step1",
                    "title": "Step 1 duplicate",
                    "type": "collect",
                    "instruction": "Do 1 again",
                    "required": True,
                    "priority": 20,
                    "prerequisites": [],
                    "captures": [{"field": "f2", "kind": "string"}],
                    "transitions": [{"when": {"operator": "is_present", "field": "f2"}, "to": "end"}],
                },
                {
                    "id": "end",
                    "title": "End",
                    "type": "end",
                    "instruction": "Finish",
                    "required": False,
                    "priority": 100,
                    "prerequisites": [],
                    "captures": [],
                    "transitions": [],
                    "messages": [{"id": "m1", "en": "Bye", "hi": "नमस्ते"}],
                },
            ],
        }
        diagnostics = validate_executable(flow)
        codes = [d.code for d in diagnostics if d.severity == "error"]
        assert "DUPLICATE_STEP" in codes

    def test_duplicate_field_across_steps(self) -> None:
        flow = {
            "schema_version": 1,
            "entry_step_id": "step1",
            "exit_step_id": "end",
            "steps": [
                {
                    "id": "step1",
                    "title": "Step 1",
                    "type": "collect",
                    "instruction": "Do 1",
                    "required": True,
                    "priority": 10,
                    "prerequisites": [],
                    "captures": [{"field": "shared_field", "kind": "string"}],
                    "transitions": [{"when": {"operator": "is_present", "field": "shared_field"}, "to": "step2"}],
                },
                {
                    "id": "step2",
                    "title": "Step 2",
                    "type": "collect",
                    "instruction": "Do 2",
                    "required": True,
                    "priority": 20,
                    "prerequisites": [],
                    "captures": [{"field": "shared_field", "kind": "string"}],
                    "transitions": [{"when": {"operator": "is_present", "field": "shared_field"}, "to": "end"}],
                },
                {
                    "id": "end",
                    "title": "End",
                    "type": "end",
                    "instruction": "Finish",
                    "required": False,
                    "priority": 100,
                    "prerequisites": [],
                    "captures": [],
                    "transitions": [],
                    "messages": [{"id": "m1", "en": "Bye", "hi": "नमस्ते"}],
                },
            ],
        }
        diagnostics = validate_executable(flow)
        codes = [d.code for d in diagnostics if d.severity == "error"]
        assert "DUPLICATE_FIELD" in codes

    def test_unknown_field_in_template_placeholder(self) -> None:
        flow = {
            "schema_version": 1,
            "entry_step_id": "summary",
            "exit_step_id": "summary",
            "steps": [
                {
                    "id": "summary",
                    "title": "Summary",
                    "type": "end",
                    "instruction": "Summary",
                    "required": False,
                    "priority": 100,
                    "prerequisites": [],
                    "captures": [],
                    "transitions": [],
                    "messages": [
                        {
                            "id": "m1",
                            "en": "Value is {{nonexistent_field}}",
                            "hi": "मान {{nonexistent_field}} है",
                        }
                    ],
                }
            ],
        }
        diagnostics = validate_executable(flow)
        codes = [d.code for d in diagnostics if d.severity == "error"]
        assert "UNKNOWN_FIELD" in codes

    def test_diagnostic_sort_order(self) -> None:
        d1 = FlowDiagnostic(code="B_CODE", path="/steps/1", message="Msg B")
        d2 = FlowDiagnostic(code="A_CODE", path="/steps/1", message="Msg A")
        d3 = FlowDiagnostic(code="C_CODE", path="/steps/0", message="Msg C")
        sorted_diags = sorted([d1, d2, d3], key=lambda d: d.sort_key())
        assert sorted_diags == [d3, d2, d1]
