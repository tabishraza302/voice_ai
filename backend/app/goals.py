from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any


JsonScalar = str | int | float | bool | None

_JSON_NUMBER = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z"
)


@dataclass(frozen=True)
class FieldCondition:
    field_key: str
    operator: str
    operand: Any


@dataclass(frozen=True)
class GoalField:
    field_key: str
    description: str
    options: tuple[str, ...] = ()
    required: bool = False
    required_when: tuple[FieldCondition, ...] = ()
    sort_order: int = 0


@dataclass(frozen=True)
class GoalOutcome:
    label: str
    conditions: tuple[FieldCondition, ...] = ()
    required_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoalState:
    fields: tuple[GoalField, ...]
    outcomes: tuple[GoalOutcome, ...]
    answers: Mapping[str, Any]
    completed_outcomes: tuple[str, ...]
    language: str


@dataclass(frozen=True)
class GoalEvaluation:
    needed_fields: tuple[GoalField, ...]
    correction_fields: tuple[GoalField, ...]
    reached_outcome: str | None
    all_done: bool


class GoalConfigurationError(ValueError):
    pass


# ── Condition evaluation (ported from workflow.py) ──

def _scalar_equal(actual: JsonScalar, operand: Any) -> bool:
    if isinstance(actual, bool) or isinstance(operand, bool):
        return isinstance(actual, bool) and isinstance(operand, bool) and actual is operand
    if isinstance(actual, str) and isinstance(operand, str):
        return actual.strip().lower() == operand.strip().lower()
    if isinstance(actual, (int, float)) and isinstance(operand, (int, float)):
        return actual == operand
    if (
        isinstance(actual, str) and isinstance(operand, (int, float))
        or isinstance(operand, str) and isinstance(actual, (int, float))
    ):
        label = actual if isinstance(actual, str) else operand
        number = operand if isinstance(actual, str) else actual
        numeric_label = label.strip()
        if not _JSON_NUMBER.fullmatch(numeric_label):
            return False
        try:
            return Decimal(numeric_label) == Decimal(str(number))
        except InvalidOperation:
            return False
    return False


def _compare(actual: JsonScalar, operator: str, operand: Any) -> bool:
    if actual is None:
        return False
    if operator == "eq":
        return _scalar_equal(actual, operand)
    if operator == "ne":
        return not _scalar_equal(actual, operand)
    if operator == "in":
        operands = operand if isinstance(operand, (list, tuple)) else [operand]
        return any(_compare(actual, "eq", item) for item in operands)
    if operator in {"gt", "gte", "lt", "lte"}:
        if isinstance(actual, bool) or isinstance(operand, bool):
            return False
        try:
            return {
                "gt": actual > operand,
                "gte": actual >= operand,
                "lt": actual < operand,
                "lte": actual <= operand,
            }[operator]
        except (TypeError, ValueError):
            return False
    return False


def _condition_met(condition: FieldCondition, answers: Mapping[str, Any]) -> bool:
    if condition.field_key not in answers:
        return False
    return _compare(answers[condition.field_key], condition.operator, condition.operand)


def _all_conditions_met(conditions: Sequence[FieldCondition], answers: Mapping[str, Any]) -> bool:
    return all(_condition_met(c, answers) for c in conditions)


# ── Field activation ──

def _field_is_needed(field: GoalField, answers: Mapping[str, Any]) -> bool:
    """Return True if this field should be collected this turn."""
    if field.field_key in answers:
        return False
    if field.required:
        return True
    if not field.required_when:
        return False
    # All dependency field_keys referenced in conditions must themselves have values.
    dependency_keys = {c.field_key for c in field.required_when}
    if not all(k in answers for k in dependency_keys):
        return False
    return _all_conditions_met(field.required_when, answers)


# ── Public API ──

def evaluate_goals(state: GoalState) -> GoalEvaluation:
    needed: list[GoalField] = []
    corrections: list[GoalField] = []

    for field in state.fields:
        if _field_is_needed(field, state.answers):
            needed.append(field)
        elif field.field_key in state.answers:
            corrections.append(field)

    reached: str | None = None
    for outcome in state.outcomes:
        if outcome.label in state.completed_outcomes:
            continue
        if not _all_conditions_met(outcome.conditions, state.answers):
            continue
        if all(fk in state.answers for fk in outcome.required_fields):
            reached = outcome.label
            break

    all_done = len(needed) == 0 and reached is None

    return GoalEvaluation(
        needed_fields=tuple(needed),
        correction_fields=tuple(corrections),
        reached_outcome=reached,
        all_done=all_done,
    )


def apply_answers(state: GoalState, extracted: Mapping[str, Any]) -> GoalState:
    valid_keys = {f.field_key for f in state.fields}
    new_answers = dict(state.answers)
    for key, value in extracted.items():
        if key in valid_keys and value is not None:
            new_answers[key] = value
    return replace(state, answers=MappingProxyType(new_answers))


def reset_for_next_request(state: GoalState, reached_outcome: str) -> GoalState:
    """Clear conditional fields, keep universal ones, record the outcome."""
    keep: dict[str, Any] = {}
    for field in state.fields:
        if field.required and field.field_key in state.answers:
            keep[field.field_key] = state.answers[field.field_key]
    return replace(
        state,
        answers=MappingProxyType(keep),
        completed_outcomes=state.completed_outcomes + (reached_outcome,),
    )


def build_tool_schema(evaluation: GoalEvaluation) -> dict[str, Any] | None:
    """Build the save_answers tool JSON schema from needed + correction fields."""
    all_fields = list(evaluation.needed_fields) + list(evaluation.correction_fields)
    if not all_fields:
        return None
    properties: dict[str, Any] = {}
    for field in all_fields:
        prop: dict[str, Any] = {"type": "string"}
        if field.options:
            prop["enum"] = list(field.options)
        if field.description:
            prop["description"] = field.description
        properties[field.field_key] = prop
    return {
        "type": "function",
        "function": {
            "name": "save_answers",
            "description": "Save answers extracted from the caller's speech.",
            "parameters": {
                "type": "object",
                "properties": properties,
            },
        },
    }
