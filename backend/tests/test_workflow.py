from __future__ import annotations

import uuid

from app.workflow import (
    FieldExtraction,
    advance_workflow,
    mark_output_completed,
    mark_output_interrupted,
    mark_output_started,
    start_workflow,
)


def step(
    field_key: str,
    sort_order: int,
    answer_type: str,
    *,
    enum_values: list[str] | None = None,
    validation_rules: list[dict] | None = None,
    branches: list[dict] | None = None,
    validation_failure_target: dict | None = None,
    wording_mode: str = "flexible",
    phrases: dict[str, str] | None = None,
    required: bool = True,
) -> dict:
    step_id = str(uuid.uuid5(uuid.NAMESPACE_URL, field_key))
    return {
        "id": step_id,
        "agent_id": str(uuid.UUID(int=1)),
        "field_key": field_key,
        "sort_order": sort_order,
        "title": field_key.replace("_", " ").title(),
        "objective": f"Collect {field_key}.",
        "required": required,
        "enabled": True,
        "answer_type": answer_type,
        "enum_values": enum_values or [],
        "wording_mode": wording_mode,
        "localized_phrases": phrases or {},
        "extraction_instructions": f"Extract {field_key}.",
        "clarification_instructions": f"Clarify {field_key}.",
        "validation_rules": validation_rules or [],
        "branches": branches or [],
        "validation_failure_target": validation_failure_target,
    }


def outcome(field_key: str, sort_order: int) -> dict:
    return step(field_key, sort_order, "outcome")


def branch(*conditions: dict, target_step_id: str | None = None, target_outcome: str | None = None) -> dict:
    return {
        "conditions": list(conditions),
        "target_step_id": target_step_id,
        "outcome": target_outcome,
    }


def condition(field_key: str, operator: str, operand: object) -> dict:
    return {"field_key": field_key, "operator": operator, "operand": operand}


def value(value: object, *, correction: bool = False) -> FieldExtraction:
    return FieldExtraction(status="value", value=value, is_correction=correction)


def missing() -> FieldExtraction:
    return FieldExtraction(status="missing")


def ambiguous(*, correction: bool = False) -> FieldExtraction:
    return FieldExtraction(status="ambiguous", is_correction=correction)


def eligibility_steps() -> list[dict]:
    age = step(
        "age",
        1,
        "number",
        validation_rules=[{"operator": "gt", "operand": 24}],
        validation_failure_target={"target_step_id": None, "outcome": "ineligible"},
    )
    salary = step(
        "monthly_salary",
        2,
        "currency",
        validation_rules=[{"operator": "gt", "operand": "6000.00"}],
        validation_failure_target={"target_step_id": None, "outcome": "ineligible"},
    )
    employed = step(
        "is_salaried",
        0,
        "boolean",
        branches=[
            branch(condition("is_salaried", "eq", True), target_step_id=age["id"]),
            branch(condition("is_salaried", "eq", False), target_outcome="ineligible"),
        ],
    )
    return [employed, age, salary, outcome("eligible", 3), outcome("ineligible", 4)]


def test_enum_candidates_must_match_the_frozen_choice_list():
    choice = step("plan", 0, "enum", enum_values=["basic", "premium"])
    rejected = advance_workflow(
        start_workflow([choice], "en").state,
        {"plan": value("enterprise")},
    )

    assert rejected.state.answers == {}
    assert rejected.action.kind == "clarify"
    assert rejected.action.reason == "invalid_type"

    accepted = advance_workflow(
        start_workflow([choice], "en").state,
        {"plan": value("premium")},
    )

    assert accepted.state.answers == {"plan": "premium"}
    assert accepted.action.kind == "complete"


def answer_all(values: dict[str, object]) -> dict[str, FieldExtraction]:
    return {key: value(candidate) for key, candidate in values.items()}


def test_eligible_multi_field_turn_validates_and_finishes_without_reasking():
    result = start_workflow(eligibility_steps(), "en")
    result = advance_workflow(
        result.state,
        answer_all({"is_salaried": True, "age": 27, "monthly_salary": "24000"}),
    )

    assert result.state.answers == {
        "is_salaried": True,
        "age": 27,
        "monthly_salary": "24000",
    }
    assert result.state.outcome == "eligible"
    assert result.state.completed is True
    assert result.action.kind == "outcome"
    assert result.action.content == "eligible"
    assert [event["field_key"] for event in result.events if event["type"] == "answer"] == [
        "is_salaried",
        "age",
        "monthly_salary",
    ]


def test_business_rule_boundaries_take_the_explicit_failure_outcome():
    for age_value, salary_value in [(24, "6000"), (25, "6000")]:
        result = start_workflow(eligibility_steps(), "en")
        result = advance_workflow(
            result.state,
            answer_all(
                {"is_salaried": True, "age": age_value, "monthly_salary": salary_value}
            ),
        )

        assert result.state.outcome == "ineligible"
        rejected = [event for event in result.events if event["type"] == "answer"]
        assert rejected[-1]["accepted"] is False
        assert rejected[-1]["field_key"] == ("age" if age_value == 24 else "monthly_salary")
        assert result.action.kind == "outcome"
        assert result.action.content == "ineligible"


def test_failed_rule_can_route_to_an_explicit_failure_step():
    follow_up = step("follow_up", 1, "text")
    age = step(
        "age",
        0,
        "number",
        validation_rules=[{"operator": "gt", "operand": 24}],
        validation_failure_target={"target_step_id": follow_up["id"], "outcome": None},
    )
    result = start_workflow([age, follow_up, outcome("complete", 2)], "en")
    failed = advance_workflow(result.state, {"age": value(18)})

    assert failed.state.answers == {}
    assert failed.state.current_step_id == follow_up["id"]
    assert failed.action.kind == "ask"
    assert failed.action.field_key == "follow_up"


def test_ambiguous_required_value_clarifies_without_taking_failure_route():
    result = start_workflow(eligibility_steps(), "en")
    result = advance_workflow(
        result.state,
        {"is_salaried": ambiguous()},
    )

    assert result.state.answers == {}
    assert result.state.outcome is None
    assert result.action.kind == "clarify"
    assert result.action.field_key == "is_salaried"
    assert result.action.reason == "ambiguous"
    assert result.events == ()


def test_changed_answer_revalidates_and_invalidates_stale_downstream_outcome():
    result = start_workflow(eligibility_steps(), "en")
    result = advance_workflow(
        result.state,
        answer_all({"is_salaried": True, "age": 27, "monthly_salary": "24000"}),
    )
    assert result.state.outcome == "eligible"

    corrected = advance_workflow(
        result.state,
        {"age": value(24, correction=True)},
    )

    assert corrected.state.answers == {"is_salaried": True}
    assert corrected.state.outcome == "ineligible"
    assert any(
        event["type"] == "answer_invalidated" and event["field_key"] == "monthly_salary"
        for event in corrected.events
    )
    assert any(
        event["type"] == "outcome_invalidated" and event["value"] == "eligible"
        for event in corrected.events
    )
    assert any(
        event["type"] == "answer"
        and event["field_key"] == "age"
        and event["accepted"] is False
        for event in corrected.events
    )
    assert corrected.action.kind == "outcome"
    assert corrected.action.content == "ineligible"


def test_ambiguous_correction_preserves_confirmed_answer_until_clarified():
    result = start_workflow(eligibility_steps(), "en")
    result = advance_workflow(
        result.state,
        answer_all({"is_salaried": True, "age": 27}),
    )

    correction = advance_workflow(
        result.state,
        {"age": ambiguous(correction=True)},
    )

    assert correction.state.answers["age"] == 27
    assert correction.action.kind == "clarify"
    assert correction.action.field_key == "age"
    assert correction.action.reason == "ambiguous"


def test_branching_uses_typed_comparisons_and_first_matching_branch():
    risk = step(
        "risk",
        0,
        "number",
        branches=[
            branch(condition("risk", "gt", 24), target_outcome="first"),
            branch(condition("risk", "gt", 10), target_outcome="second"),
            branch(target_outcome="fallback"),
        ],
    )
    result = start_workflow(
        [risk, outcome("first", 1), outcome("second", 2), outcome("fallback", 3)],
        "en",
    )
    result = advance_workflow(result.state, {"risk": value(27)})

    assert result.state.outcome == "first"
    assert result.action.content == "first"


def test_invalid_or_non_finite_extraction_is_rejected_for_clarification():
    number = step(
        "age",
        0,
        "number",
        validation_rules=[{"operator": "gt", "operand": 24}],
        validation_failure_target={"target_step_id": None, "outcome": "ineligible"},
    )
    result = start_workflow([number, outcome("ineligible", 1)], "en")
    result = advance_workflow(result.state, {"age": value(float("inf"))})

    assert result.state.answers == {}
    assert result.state.outcome is None
    assert result.action.kind == "clarify"
    assert result.action.reason == "invalid_type"
    assert result.events[0]["accepted"] is False


def test_exact_opening_is_completed_only_after_output_and_resumes_after_interrupt():
    exact = step(
        "name",
        0,
        "text",
        wording_mode="exact",
        phrases={"en": "What is your name?"},
    )
    started = start_workflow([exact], "en")

    assert started.action.kind == "exact_opening"
    assert started.action.content == "What is your name?"
    assert started.state.opening_states[exact["id"]] == "pending"

    playing = mark_output_started(started.state, exact["id"])
    interrupted = mark_output_interrupted(playing.state, exact["id"])

    assert interrupted.state.opening_states[exact["id"]] == "interrupted"
    assert interrupted.action.kind == "exact_opening"
    assert interrupted.action.resume is True

    playing_again = mark_output_started(interrupted.state, exact["id"])
    completed = mark_output_completed(playing_again.state, exact["id"])

    assert completed.state.opening_states[exact["id"]] == "complete"
    follow_up = advance_workflow(completed.state, {"name": missing()})
    assert follow_up.action.kind == "clarify"
    assert follow_up.action.kind != "exact_opening"


def test_informational_message_and_outcome_are_each_emitted_once():
    message = step("intro", 0, "message", wording_mode="flexible")
    name = step("name", 1, "text")
    result = start_workflow([message, name, outcome("complete", 2)], "en")

    assert result.action.kind == "message"
    assert result.action.step_id == message["id"]
    assert result.state.current_step_id == name["id"]

    next_turn = advance_workflow(result.state, {"name": missing()})
    assert next_turn.action.kind == "clarify"
    finished = advance_workflow(next_turn.state, {"name": value("Ari")})
    assert finished.action.kind == "outcome"
    assert finished.state.outcome == "complete"
    repeated = advance_workflow(finished.state, {"name": missing()})
    assert repeated.action is None
    assert repeated.events == ()
