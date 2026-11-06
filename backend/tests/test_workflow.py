from __future__ import annotations

import uuid

from app.workflow import (
    FieldExtraction,
    advance_workflow,
    start_workflow,
)


def step(
    field_key: str,
    sort_order: int,
    *,
    title: str | None = None,
    objective: str | None = None,
    branches: list[dict] | None = None,
) -> dict:
    step_id = str(uuid.uuid5(uuid.NAMESPACE_URL, field_key))
    return {
        "id": step_id,
        "agent_id": str(uuid.UUID(int=1)),
        "field_key": field_key,
        "sort_order": sort_order,
        "title": title or field_key.replace("_", " ").title(),
        "objective": objective or f"Collect {field_key}.",
        "branches": branches or [],
    }


def branch(*conditions: dict, target_step_id: str | None = None, target_outcome: str | None = None) -> dict:
    return {
        "conditions": list(conditions),
        "target_step_id": target_step_id,
        "outcome": target_outcome,
    }


def condition(field_key: str, operator: str, operand: object) -> dict:
    return {"field_key": field_key, "operator": operator, "operand": operand}


def value(val: object, *, correction: bool = False) -> FieldExtraction:
    return FieldExtraction(status="value", value=val, is_correction=correction)


def missing() -> FieldExtraction:
    return FieldExtraction(status="missing")


def ambiguous(*, correction: bool = False) -> FieldExtraction:
    return FieldExtraction(status="ambiguous", is_correction=correction)


def test_start_workflow_begins_at_first_step_and_emits_ask():
    first = step("greeting", 0, title="Greeting", objective="Say hello")
    second = step("name", 1, title="Name", objective="Ask caller name")
    transition = start_workflow([first, second], "en")

    assert transition.state.current_step_id == first["id"]
    assert transition.state.answers == {}
    assert transition.action is not None
    assert transition.action.kind == "ask"
    assert transition.action.step_id == first["id"]
    assert transition.action.field_key == "greeting"


def test_visual_flow_branch_matches_conditional_answer_and_routes_to_target_step():
    interested = step("details", 1, title="Details", objective="Discuss details")
    greeting = step(
        "intro",
        0,
        title="Intro",
        objective="Are you interested?",
        branches=[
            branch(condition("intro", "eq", "yes"), target_step_id=interested["id"]),
            branch(condition("intro", "eq", "no"), target_outcome="end"),
        ],
    )
    result = start_workflow([greeting, interested], "en")
    result = advance_workflow(result.state, {"intro": value("yes")})

    assert result.state.answers == {"intro": "yes"}
    assert result.state.current_step_id == interested["id"]
    assert result.action is not None
    assert result.action.kind == "ask"
    assert result.action.step_id == interested["id"]
    assert result.action.field_key == "details"


def test_visual_flow_branch_matches_case_insensitively():
    interested = step("details", 1, title="Details", objective="Discuss details")
    greeting = step(
        "intro",
        0,
        branches=[
            branch(condition("intro", "eq", "Yes"), target_step_id=interested["id"]),
            branch(target_outcome="end"),
        ],
    )
    result = start_workflow([greeting, interested], "en")
    result = advance_workflow(result.state, {"intro": value("yes")})

    assert result.state.answers == {"intro": "yes"}
    assert result.state.current_step_id == interested["id"]
    assert result.action is not None
    assert result.action.kind == "ask"

def test_visual_flow_conditional_branch_after_fallback_still_matches():
    interested = step("details", 1)
    greeting = step(
        "intro",
        0,
        branches=[
            branch(target_outcome="end"),
            branch(condition("intro", "eq", "yes"), target_step_id=interested["id"]),
        ],
    )
    result = start_workflow([greeting, interested], "en")
    result = advance_workflow(result.state, {"intro": value("yes")})

    assert result.state.current_step_id == interested["id"]
    assert result.state.outcome is None


def test_visual_flow_unknown_operator_does_not_match_equal_value():
    interested = step("details", 1)
    greeting = step(
        "intro",
        0,
        branches=[
            branch(target_outcome="end"),
            branch(condition("intro", "bogus", "yes"), target_step_id=interested["id"]),
        ],
    )
    result = start_workflow([greeting, interested], "en")
    result = advance_workflow(result.state, {"intro": value("yes")})

    assert result.state.outcome == "end"
    assert result.state.completed is True


def test_visual_flow_equality_matches_numeric_answer_to_string_label():
    adult = step("adult_details", 1)
    age = step(
        "age",
        0,
        branches=[
            branch(target_outcome="end"),
            branch(condition("age", "eq", "18"), target_step_id=adult["id"]),
        ],
    )
    result = start_workflow([age, adult], "en")
    result = advance_workflow(result.state, {"age": value(18)})

    assert result.state.current_step_id == adult["id"]
    assert result.state.outcome is None


def test_visual_flow_equality_does_not_match_boolean_to_integer():
    interested = step("details", 1)
    answer = step(
        "answer",
        0,
        branches=[
            branch(condition("answer", "eq", 1), target_step_id=interested["id"]),
            branch(target_outcome="end"),
        ],
    )
    result = start_workflow([answer, interested], "en")
    result = advance_workflow(result.state, {"answer": value(True)})

    assert result.state.outcome == "end"
    assert result.state.completed is True



def test_visual_flow_fallback_other_branch_takes_fallback_target():
    interested = step("details", 1, title="Details", objective="Discuss details")
    greeting = step(
        "intro",
        0,
        branches=[
            branch(condition("intro", "eq", "yes"), target_step_id=interested["id"]),
            branch(target_outcome="end"),
        ],
    )
    result = start_workflow([greeting, interested], "en")
    result = advance_workflow(result.state, {"intro": value("maybe")})

    assert result.state.answers == {"intro": "maybe"}
    assert result.state.outcome == "end"
    assert result.state.completed is True
    assert result.action is not None
    assert result.action.kind == "outcome"
    assert result.action.content == "end"


def test_visual_flow_advances_to_next_step_in_sort_order_when_no_branch_matches():
    first = step("step_one", 0)
    second = step("step_two", 1)
    result = start_workflow([first, second], "en")
    result = advance_workflow(result.state, {"step_one": value("acknowledged")})

    assert result.state.answers == {"step_one": "acknowledged"}
    assert result.state.current_step_id == second["id"]
    assert result.action is not None
    assert result.action.kind == "ask"
    assert result.action.step_id == second["id"]


def test_workflow_completes_at_end_of_flow():
    only = step("confirm", 0)
    result = start_workflow([only], "en")
    result = advance_workflow(result.state, {"confirm": value("done")})

    assert result.state.answers == {"confirm": "done"}
    assert result.state.completed is True
    assert result.action is not None
    assert result.action.kind == "complete"


def test_workflow_completes_when_branch_targets_end_outcome():
    intro = step(
        "intro",
        0,
        branches=[
            branch(condition("intro", "eq", "no"), target_outcome="end"),
        ],
    )
    follow_up = step("follow_up", 1)
    result = start_workflow([intro, follow_up], "en")
    result = advance_workflow(result.state, {"intro": value("no")})

    assert result.state.outcome == "end"
    assert result.state.completed is True
    assert result.action is not None
    assert result.action.kind == "outcome"
    assert result.action.content == "end"


def test_correction_invalidates_downstream_answers_and_reroutes():
    step_b = step("path_b", 1)
    step_c = step("path_c", 2)
    step_a = step(
        "choice",
        0,
        branches=[
            branch(condition("choice", "eq", "b"), target_step_id=step_b["id"]),
            branch(condition("choice", "eq", "c"), target_step_id=step_c["id"]),
        ],
    )
    result = start_workflow([step_a, step_b, step_c], "en")
    result = advance_workflow(result.state, {"choice": value("b")})
    assert result.state.current_step_id == step_b["id"]

    result = advance_workflow(result.state, {"path_b": value("b_details")})
    assert result.state.answers == {"choice": "b", "path_b": "b_details"}

    # Correction on step A from 'b' to 'c'
    corrected = advance_workflow(result.state, {"choice": value("c", correction=True)})
    assert corrected.state.answers == {"choice": "c"}
    assert corrected.state.current_step_id == step_c["id"]
    assert any(
        event["type"] == "answer_invalidated" and event["field_key"] == "path_b"
        for event in corrected.events
    )


def test_unanswered_or_ambiguous_step_emits_ask():
    first = step("first", 0)
    result = start_workflow([first], "en")

    missing_turn = advance_workflow(result.state, {"first": missing()})
    assert missing_turn.state.answers == {}
    assert missing_turn.action is not None
    assert missing_turn.action.kind == "ask"
    assert missing_turn.action.step_id == first["id"]

    ambiguous_turn = advance_workflow(result.state, {"first": ambiguous()})
    assert ambiguous_turn.state.answers == {}
    assert ambiguous_turn.action is not None
    assert ambiguous_turn.action.kind == "ask"
    assert ambiguous_turn.action.step_id == first["id"]
