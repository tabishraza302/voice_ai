import pytest
from app.goals import (
    FieldCondition, GoalField, GoalOutcome, GoalState, GoalEvaluation,
    evaluate_goals, apply_answers, reset_for_next_request,
    build_tool_schema,
)

def field(key, *, required=False, required_when=(), options=(), description="", sort_order=0):
    return GoalField(
        field_key=key,
        description=description or f"Collect {key}",
        options=tuple(options),
        required=required,
        required_when=tuple(required_when),
        sort_order=sort_order,
    )

def when(field_key, operator="eq", operand=None):
    return FieldCondition(field_key=field_key, operator=operator, operand=operand)

def outcome(label, conditions=(), required_fields=()):
    return GoalOutcome(label=label, conditions=tuple(conditions), required_fields=tuple(required_fields))

def state(fields, outcomes=(), answers=None, completed=()):
    return GoalState(
        fields=tuple(fields),
        outcomes=tuple(outcomes),
        answers=answers or {},
        completed_outcomes=tuple(completed),
        language="en",
    )

def test_always_required_field_is_needed_when_empty():
    s = state([field("name", required=True)])
    ev = evaluate_goals(s)
    assert len(ev.needed_fields) == 1
    assert ev.needed_fields[0].field_key == "name"

def test_always_required_field_not_needed_when_filled():
    s = state([field("name", required=True)], answers={"name": "Alice"})
    ev = evaluate_goals(s)
    assert len(ev.needed_fields) == 0

def test_conditional_field_skipped_when_dependency_missing():
    s = state([
        field("intent", required=True, options=["order", "cancel"]),
        field("items", required_when=[when("intent", "eq", "order")]),
    ])
    ev = evaluate_goals(s)
    keys = [f.field_key for f in ev.needed_fields]
    assert "intent" in keys
    assert "items" not in keys

def test_conditional_field_activated_when_dependency_met():
    s = state(
        [
            field("intent", required=True, options=["order", "cancel"]),
            field("items", required_when=[when("intent", "eq", "order")]),
        ],
        answers={"intent": "order"},
    )
    ev = evaluate_goals(s)
    keys = [f.field_key for f in ev.needed_fields]
    assert "intent" not in keys
    assert "items" in keys

def test_conditional_field_inactive_when_dependency_wrong_value():
    s = state(
        [
            field("intent", required=True, options=["order", "cancel"]),
            field("items", required_when=[when("intent", "eq", "order")]),
            field("order_id", required_when=[when("intent", "eq", "cancel")]),
        ],
        answers={"intent": "cancel"},
    )
    ev = evaluate_goals(s)
    keys = [f.field_key for f in ev.needed_fields]
    assert "items" not in keys
    assert "order_id" in keys

def test_outcome_reached_when_conditions_and_fields_met():
    s = state(
        [
            field("intent", required=True, options=["order"]),
            field("items", required_when=[when("intent", "eq", "order")]),
        ],
        outcomes=[outcome("order_placed", conditions=[when("intent", "eq", "order")], required_fields=["items"])],
        answers={"intent": "order", "items": "2 pizzas"},
    )
    ev = evaluate_goals(s)
    assert ev.reached_outcome == "order_placed"

def test_outcome_not_reached_when_field_missing():
    s = state(
        [
            field("intent", required=True, options=["order"]),
            field("items", required_when=[when("intent", "eq", "order")]),
        ],
        outcomes=[outcome("order_placed", conditions=[when("intent", "eq", "order")], required_fields=["items"])],
        answers={"intent": "order"},
    )
    ev = evaluate_goals(s)
    assert ev.reached_outcome is None

def test_already_completed_outcome_not_repeated():
    s = state(
        [field("intent", required=True, options=["order"])],
        outcomes=[outcome("order_placed", conditions=[when("intent", "eq", "order")])],
        answers={"intent": "order"},
        completed=["order_placed"],
    )
    ev = evaluate_goals(s)
    assert ev.reached_outcome is None

def test_correction_fields_include_answered_fields():
    s = state(
        [
            field("intent", required=True, options=["order", "cancel"]),
            field("items", required_when=[when("intent", "eq", "order")]),
        ],
        answers={"intent": "order", "items": "2 pizzas"},
    )
    ev = evaluate_goals(s)
    correction_keys = [f.field_key for f in ev.correction_fields]
    assert "intent" in correction_keys
    assert "items" in correction_keys

def test_apply_answers_adds_new():
    s = state([field("name", required=True)])
    s2 = apply_answers(s, {"name": "Alice"})
    assert s2.answers["name"] == "Alice"

def test_apply_answers_ignores_unknown_keys():
    s = state([field("name", required=True)])
    s2 = apply_answers(s, {"name": "Alice", "bogus": "x"})
    assert "bogus" not in s2.answers

def test_apply_answers_overwrites_existing():
    s = state([field("name", required=True)], answers={"name": "Alice"})
    s2 = apply_answers(s, {"name": "Bob"})
    assert s2.answers["name"] == "Bob"

def test_reset_keeps_universal_clears_conditional():
    s = state(
        [
            field("name", required=True),
            field("intent", required=True, options=["order", "cancel"]),
            field("items", required_when=[when("intent", "eq", "order")]),
        ],
        answers={"name": "Alice", "intent": "order", "items": "pizza"},
    )
    s2 = reset_for_next_request(s, "order_placed")
    assert s2.answers.get("name") == "Alice"
    assert s2.answers.get("intent") == "order"  # required=True, kept
    assert "items" not in s2.answers
    assert "order_placed" in s2.completed_outcomes

def test_build_tool_schema_includes_enum_options():
    s = state([field("intent", required=True, options=["order", "cancel"])])
    ev = evaluate_goals(s)
    schema = build_tool_schema(ev)
    assert schema is not None
    props = schema["function"]["parameters"]["properties"]
    assert props["intent"]["enum"] == ["order", "cancel"]

def test_build_tool_schema_returns_none_when_no_fields():
    s = state(
        [field("intent", required=True)],
        answers={"intent": "order"},
    )
    ev = evaluate_goals(s)
    schema = build_tool_schema(ev)
    # intent is answered, no needed fields, but correction_fields should have it
    # so schema should not be None
    assert schema is not None

def test_build_tool_schema_returns_none_when_truly_empty():
    ev = GoalEvaluation(
        needed_fields=(), correction_fields=(), reached_outcome=None, all_done=True
    )
    schema = build_tool_schema(ev)
    assert schema is None

def test_all_done_true_when_nothing_left():
    s = state(
        [field("intent", required=True, options=["order"])],
        outcomes=[outcome("done", conditions=[when("intent", "eq", "order")])],
        answers={"intent": "order"},
        completed=["done"],
    )
    ev = evaluate_goals(s)
    assert ev.all_done is True

def test_nested_conditions():
    """Two levels: intent activates order_type, order_type activates delivery_addr."""
    s = state(
        [
            field("intent", required=True, options=["order"]),
            field("order_type", required_when=[when("intent", "eq", "order")], options=["delivery", "pickup"]),
            field("delivery_addr", required_when=[when("order_type", "eq", "delivery")]),
        ],
        answers={"intent": "order", "order_type": "delivery"},
    )
    ev = evaluate_goals(s)
    keys = [f.field_key for f in ev.needed_fields]
    assert "delivery_addr" in keys
    assert "intent" not in keys
    assert "order_type" not in keys
