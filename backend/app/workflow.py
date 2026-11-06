from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Literal


JsonScalar = str | int | float | bool | None
ExtractionStatus = Literal["value", "missing", "ambiguous"]
_JSON_NUMBER = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z"
)


@dataclass(frozen=True)
class FieldExtraction:
    status: ExtractionStatus
    value: JsonScalar = None
    is_correction: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"value", "missing", "ambiguous"}:
            raise ValueError("status must be value, missing, or ambiguous")
        if (self.status == "value") == (self.value is None):
            raise ValueError("value status requires a value and other statuses require null")
        if not isinstance(self.value, (str, int, float, bool, type(None))):
            raise ValueError("extracted values must be JSON scalars")
        if not isinstance(self.is_correction, bool):
            raise ValueError("is_correction must be a boolean")


@dataclass(frozen=True)
class BranchCondition:
    field_key: str
    operator: str
    operand: Any


@dataclass(frozen=True)
class StepBranch:
    conditions: tuple[BranchCondition, ...]
    target_step_id: str | None
    outcome: str | None


@dataclass(frozen=True)
class WorkflowTarget:
    target_step_id: str | None
    outcome: str | None


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    field_key: str
    sort_order: int
    title: str
    objective: str
    branches: tuple[StepBranch, ...]


@dataclass(frozen=True)
class WorkflowState:
    steps: tuple[WorkflowStep, ...]
    answers: Mapping[str, JsonScalar]
    language: str
    current_step_id: str | None
    outcome: str | None = None
    completed: bool = False
    pending_corrections: frozenset[str] = frozenset()


@dataclass(frozen=True)
class WorkflowAction:
    kind: Literal[
        "ask",
        "outcome",
        "complete",
    ]
    step_id: str | None = None
    field_key: str | None = None
    content: str | None = None
    reason: str | None = None
    resume: bool = False


@dataclass(frozen=True)
class WorkflowTransition:
    state: WorkflowState
    events: tuple[dict[str, Any], ...] = ()
    action: WorkflowAction | None = None


class WorkflowConfigurationError(ValueError):
    pass


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _freeze_step(raw: Mapping[str, Any], index: int) -> WorkflowStep:
    if not isinstance(raw, Mapping):
        raise WorkflowConfigurationError(f"step {index} must be an object")

    step_id = raw.get("id")
    field_key = raw.get("field_key")
    sort_order = raw.get("sort_order")
    if not isinstance(step_id, (str, uuid.UUID)) or not str(step_id):
        raise WorkflowConfigurationError(f"step {index} needs an id")
    if not isinstance(field_key, str) or not field_key.strip():
        raise WorkflowConfigurationError(f"step {index} needs a field_key")
    if not isinstance(sort_order, int) or isinstance(sort_order, bool):
        raise WorkflowConfigurationError(f"step {step_id} needs an integer sort_order")

    branches = raw.get("branches", [])
    if not isinstance(branches, list):
        raise WorkflowConfigurationError(f"step {step_id} has malformed branches")

    frozen_branches: list[StepBranch] = []
    for branch in branches:
        if not isinstance(branch, Mapping):
            raise WorkflowConfigurationError(f"step {step_id} has a malformed branch")
        conditions = branch.get("conditions", [])
        if not isinstance(conditions, list):
            raise WorkflowConfigurationError(f"step {step_id} has malformed branch conditions")
        frozen_conditions: list[BranchCondition] = []
        for condition in conditions:
            if not isinstance(condition, Mapping):
                raise WorkflowConfigurationError(f"step {step_id} has a malformed branch condition")
            frozen_conditions.append(
                BranchCondition(
                    field_key=str(condition.get("field_key", "")),
                    operator=str(condition.get("operator", "eq")),
                    operand=_freeze_json(condition.get("operand")),
                )
            )
        target_id = branch.get("target_step_id")
        outcome = branch.get("outcome")
        frozen_branches.append(
            StepBranch(
                tuple(frozen_conditions),
                str(target_id) if target_id is not None else None,
                outcome if isinstance(outcome, str) else None,
            )
        )

    return WorkflowStep(
        id=str(step_id),
        field_key=field_key,
        sort_order=sort_order,
        title=raw.get("title") if isinstance(raw.get("title"), str) else field_key,
        objective=raw.get("objective") if isinstance(raw.get("objective"), str) else "",
        branches=tuple(frozen_branches),
    )


def _immutable_answers(values: Mapping[str, JsonScalar]) -> Mapping[str, JsonScalar]:
    return MappingProxyType(dict(values))


def _step_indexes(state: WorkflowState) -> tuple[dict[str, WorkflowStep], dict[str, WorkflowStep], dict[str, int]]:
    by_id: dict[str, WorkflowStep] = {}
    by_key: dict[str, WorkflowStep] = {}
    position: dict[str, int] = {}
    for index, step in enumerate(state.steps):
        if step.id in by_id:
            raise WorkflowConfigurationError(f"duplicate step id '{step.id}'")
        by_id[step.id] = step
        if step.field_key in by_key:
            raise WorkflowConfigurationError(f"duplicate field_key '{step.field_key}'")
        by_key[step.field_key] = step
        position[step.field_key] = index
    return by_id, by_key, position


def _next_step(state: WorkflowState, step_id: str) -> str | None:
    current_index = next((index for index, step in enumerate(state.steps) if step.id == step_id), None)
    if current_index is None:
        raise WorkflowConfigurationError(f"unknown step id '{step_id}'")
    if current_index + 1 < len(state.steps):
        return state.steps[current_index + 1].id
    return None


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


def _compare(
    actual: JsonScalar,
    operator: str,
    operand: Any,
) -> bool:
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


def _condition_matches(
    condition: BranchCondition,
    by_key: Mapping[str, WorkflowStep],
    answers: Mapping[str, JsonScalar],
) -> bool:
    if condition.field_key not in answers:
        return False
    return _compare(
        answers[condition.field_key],
        condition.operator,
        condition.operand,
    )


def _branch_target(
    step: WorkflowStep,
    by_key: Mapping[str, WorkflowStep],
    answers: Mapping[str, JsonScalar],
) -> WorkflowTarget | None:
    fallback_branch: StepBranch | None = None
    for branch in step.branches:
        if not branch.conditions:
            if fallback_branch is None:
                fallback_branch = branch
            continue
        if all(_condition_matches(condition, by_key, answers) for condition in branch.conditions):
            if (branch.target_step_id is None) == (branch.outcome is None):
                raise WorkflowConfigurationError(f"step {step.id} has a branch without exactly one target")
            return WorkflowTarget(branch.target_step_id, branch.outcome)
    if fallback_branch is not None:
        if (fallback_branch.target_step_id is None) == (fallback_branch.outcome is None):
            raise WorkflowConfigurationError(f"step {step.id} has a branch without exactly one target")
        return WorkflowTarget(fallback_branch.target_step_id, fallback_branch.outcome)
    return None


def _emit_outcome(
    state: WorkflowState,
    outcome: str,
    events: list[dict[str, Any]],
) -> WorkflowState:
    if state.outcome is not None and state.outcome != outcome:
        events.append({"type": "outcome_invalidated", "value": state.outcome})
    if state.outcome != outcome:
        events.append({"type": "outcome", "value": outcome})
    return replace(state, current_step_id=None, outcome=outcome, completed=True)


def _route_to_target(
    state: WorkflowState,
    target: WorkflowTarget,
    events: list[dict[str, Any]],
) -> WorkflowState:
    if target.target_step_id is not None:
        by_id, _, _ = _step_indexes(state)
        step = by_id.get(target.target_step_id)
        if step is None:
            raise WorkflowConfigurationError(f"workflow target '{target.target_step_id}' is missing")
        return replace(state, current_step_id=step.id, completed=False, outcome=None)
    if target.outcome is None:
        raise WorkflowConfigurationError("workflow target has no destination")
    return _emit_outcome(state, target.outcome, events)


def _present(state: WorkflowState, events: list[dict[str, Any]]) -> WorkflowTransition:
    by_id, _, _ = _step_indexes(state)
    current_step_id = state.current_step_id
    if current_step_id is not None:
        step = by_id.get(current_step_id)
        if step is None:
            raise WorkflowConfigurationError(f"current step '{current_step_id}' is missing")
        return WorkflowTransition(
            state,
            tuple(events),
            WorkflowAction(kind="ask", step_id=step.id, field_key=step.field_key),
        )

    if not state.completed:
        state = replace(state, current_step_id=None, completed=True)
        return WorkflowTransition(state, tuple(events), WorkflowAction(kind="complete"))
    return WorkflowTransition(state, tuple(events), None)


def start_workflow(step_snapshot: Sequence[Mapping[str, Any]], language: str = "en") -> WorkflowTransition:
    """Freeze a published Step snapshot and initialize one call's workflow."""
    if not isinstance(language, str) or not language.strip():
        raise WorkflowConfigurationError("language must be a non-empty string")
    frozen = tuple(_freeze_step(row, index) for index, row in enumerate(step_snapshot))
    indexed = sorted(enumerate(frozen), key=lambda pair: (pair[1].sort_order, pair[0]))
    ordered = tuple(step for _, step in indexed)
    state = WorkflowState(
        steps=ordered,
        answers=_immutable_answers({}),
        language=language,
        current_step_id=ordered[0].id if ordered else None,
    )
    _step_indexes(state)
    return _present(state, [])


def advance_workflow(
    state: WorkflowState,
    extracted: Mapping[str, FieldExtraction],
) -> WorkflowTransition:
    """Apply one extractor result without database access or snapshot mutation."""
    if not isinstance(extracted, Mapping):
        raise TypeError("extracted must map field keys to FieldExtraction values")
    if any(not isinstance(item, FieldExtraction) for item in extracted.values()):
        raise TypeError("extracted values must be FieldExtraction instances")

    by_id, by_key, positions = _step_indexes(state)
    candidates = {
        key: item
        for key, item in extracted.items()
        if key in by_key and item.status != "missing"
    }
    correction_keys = [
        key
        for key, item in candidates.items()
        if item.is_correction and key in state.answers
    ]
    correction_keys.sort(key=lambda key: positions[key])
    current_id = state.current_step_id
    events: list[dict[str, Any]] = []

    if correction_keys:
        correction_key = correction_keys[0]
        correction_step = by_key[correction_key]
        current_id = correction_step.id
        state = replace(state, completed=False)

    if current_id is None:
        return WorkflowTransition(state, tuple(events), None)
    state = replace(state, current_step_id=current_id)
    visited: set[str] = set()
    while state.current_step_id is not None:
        step_id = state.current_step_id
        if step_id in visited:
            raise WorkflowConfigurationError("workflow route contains a cycle")
        visited.add(step_id)
        by_id, by_key, positions = _step_indexes(state)
        step = by_id.get(step_id)
        if step is None:
            raise WorkflowConfigurationError(f"current step '{step_id}' is missing")

        extraction = candidates.get(step.field_key)
        pending_correction = step.field_key in state.pending_corrections
        if extraction is None or extraction.status == "missing":
            if step.field_key in state.answers and not pending_correction:
                target = _branch_target(step, by_key, state.answers)
                state = _route_to_target(state, target, events) if target else replace(
                    state, current_step_id=_next_step(state, step.id)
                )
                continue
            return _present(state, events)

        if extraction.status == "ambiguous":
            if extraction.is_correction or pending_correction:
                state = replace(
                    state,
                    pending_corrections=state.pending_corrections | {step.field_key},
                )
            return _present(state, events)

        if step.field_key in state.answers and not (extraction.is_correction or pending_correction):
            target = _branch_target(step, by_key, state.answers)
            state = _route_to_target(state, target, events) if target else replace(
                state, current_step_id=_next_step(state, step.id)
            )
            continue

        candidate = extraction.value
        old_value = state.answers.get(step.field_key)
        changed_correction = (extraction.is_correction or pending_correction) and old_value != candidate
        answers = dict(state.answers)
        if extraction.is_correction or pending_correction:
            correction_position = positions[step.field_key]
            for old_key, value in tuple(answers.items()):
                old_position = positions.get(old_key)
                if old_key == step.field_key or (changed_correction and old_position is not None and old_position > correction_position):
                    old_step = by_key[old_key]
                    if old_key != step.field_key or changed_correction:
                        events.append(
                            {
                                "type": "answer_invalidated",
                                "step_id": old_step.id,
                                "field_key": old_key,
                                "value": value,
                                "reason": "correction",
                            }
                        )
                    answers.pop(old_key, None)
            if state.outcome is not None:
                events.append({"type": "outcome_invalidated", "value": state.outcome})
                state = replace(state, outcome=None, completed=False)
        answers[step.field_key] = candidate
        state = replace(
            state,
            answers=_immutable_answers(answers),
            pending_corrections=state.pending_corrections - {step.field_key},
            completed=False,
        )
        events.append(
            {
                "type": "answer",
                "step_id": step.id,
                "field_key": step.field_key,
                "value": candidate,
                "accepted": True,
                "validation_errors": [],
            }
        )
        target = _branch_target(step, by_key, state.answers)
        state = _route_to_target(state, target, events) if target else replace(
            state, current_step_id=_next_step(state, step.id)
        )
        if state.outcome is not None:
            return WorkflowTransition(
                state,
                tuple(events),
                WorkflowAction(kind="outcome", field_key=state.outcome, content=state.outcome),
            )

    return _present(state, events)
