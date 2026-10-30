"""Deterministic graph-derived Flow QA scenario contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_api.qa.service import QABranchCoverageLimitError, derive_scenarios, select_scenarios


FLOW_PATH = Path(__file__).parents[1] / "fixtures" / "flow" / "valid-loan-en-hi.json"


def _flow() -> dict:
    return json.loads(FLOW_PATH.read_text(encoding="utf-8"))["flow"]


def test_branch_and_regression_scenario_ids_are_frozen_for_a_flow() -> None:
    flow = _flow()
    first = derive_scenarios(flow, flow_hash="a" * 64, max_scenarios=64)
    again = derive_scenarios(flow, flow_hash="a" * 64, max_scenarios=64)

    assert [scenario.model_dump(mode="json") for scenario in first] == [
        scenario.model_dump(mode="json") for scenario in again
    ]
    assert len({scenario.scenario_id for scenario in first}) == len(first)
    assert all(scenario.scenario_id.startswith("qa_") for scenario in first)

    branch_edges = {
        (step["id"], index, transition["to"])
        for step in flow["steps"]
        for index, transition in enumerate(step.get("transitions", []))
    }
    derived_edges = {
        (
            scenario.expected_edge.from_step_id,
            scenario.expected_edge.transition_index,
            scenario.expected_edge.to_step_id,
        )
        for scenario in first
        if scenario.expected_edge is not None
    }
    assert derived_edges == branch_edges
    kinds = {scenario.kind for scenario in first}
    assert {"branch", "refusal", "clarification_exit", "correction"} <= kinds
    assert {
        "dependency_invalidation",
        "detour",
        "contradiction",
        "missing_business_fact",
        "early_termination",
        "prompt_injection",
    } <= kinds

    invalidation = next(
        scenario for scenario in first if scenario.kind == "dependency_invalidation"
    )
    assert invalidation.target_fields == ["employment_type", "monthly_income_inr"]
    assert invalidation.target_step_id == "salary"


def test_out_of_order_scenario_requires_multiple_captures() -> None:
    flow = _flow()
    employment = next(step for step in flow["steps"] if step["id"] == "employment")
    employment["captures"].append(
        {"field": "additional_field", "kind": "string", "required": True}
    )

    scenarios = derive_scenarios(flow, flow_hash="e" * 64, max_scenarios=64)

    scenario = next(item for item in scenarios if item.kind == "out_of_order")
    assert scenario.target_step_id == "employment"
    assert scenario.target_fields == ["employment_type", "additional_field"]
    assert {
        item.kind for item in scenarios if item.target_step_id == "employment"
    } >= {"out_of_order", "multi_answer", "partial_info"}


def test_frozen_scenarios_can_be_selected_for_an_identical_rerun() -> None:
    flow = _flow()
    original = derive_scenarios(flow, flow_hash="b" * 64, max_scenarios=64)
    selected_ids = [original[0].scenario_id, original[-1].scenario_id]

    rerun = select_scenarios(
        flow,
        flow_hash="b" * 64,
        max_scenarios=64,
        scenario_ids=selected_ids,
    )

    assert [scenario.model_dump(mode="json") for scenario in rerun] == [
        original[0].model_dump(mode="json"),
        original[-1].model_dump(mode="json"),
    ]


def test_unknown_frozen_scenario_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown scenario"):
        select_scenarios(
            _flow(),
            flow_hash="c" * 64,
            max_scenarios=64,
            scenario_ids=["qa_" + "0" * 64],
        )


def test_scenario_limit_rejects_incomplete_reachable_branch_coverage() -> None:
    flow = _flow()
    bounded = derive_scenarios(flow, flow_hash="d" * 64, max_scenarios=10)
    assert len(bounded) == 10
    assert all(scenario.kind == "branch" for scenario in bounded)

    with pytest.raises(QABranchCoverageLimitError):
        derive_scenarios(flow, flow_hash="d" * 64, max_scenarios=3)
