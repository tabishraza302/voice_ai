from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Literal


JsonScalar = str | int | float | bool | None
ExtractionStatus = Literal["value", "missing", "ambiguous"]


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
class ValidationRule:
    operator: str
    operand: Any


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
    enabled: bool
    required: bool
    answer_type: str
    wording_mode: str
    enum_values: tuple[str, ...]
    title: str
    objective: str
    extraction_instructions: str
    clarification_instructions: str
    localized_phrases: tuple[tuple[str, str], ...]
    validation_rules: tuple[ValidationRule, ...]
    branches: tuple[StepBranch, ...]
    validation_failure_target: WorkflowTarget | None

    def phrase(self, language: str) -> str | None:
        return next((phrase for code, phrase in self.localized_phrases if code == language), None)


@dataclass(frozen=True)
class WorkflowState:
    steps: tuple[WorkflowStep, ...]
    answers: Mapping[str, JsonScalar]
    language: str
    current_step_id: str | None
    outcome: str | None = None
    completed: bool = False
    completed_messages: frozenset[str] = frozenset()
    pending_corrections: frozenset[str] = frozenset()
    opening_states: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class WorkflowAction:
    kind: Literal[
        "ask",
        "clarify",
        "message",
        "exact_opening",
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


_VALUE_TYPES = frozenset({"boolean", "text", "number", "currency", "enum"})
_ANSWER_TYPES = _VALUE_TYPES | {"message", "outcome"}
_ORDER_OPERATORS = frozenset({"gt", "gte", "lt", "lte"})
_OPERATORS = _ORDER_OPERATORS | {"eq", "ne", "in"}


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
    answer_type = raw.get("answer_type")
    if not isinstance(step_id, (str, uuid.UUID)) or not str(step_id):
        raise WorkflowConfigurationError(f"step {index} needs an id")
    if not isinstance(field_key, str) or not field_key.strip():
        raise WorkflowConfigurationError(f"step {index} needs a field_key")
    if not isinstance(sort_order, int) or isinstance(sort_order, bool):
        raise WorkflowConfigurationError(f"step {step_id} needs an integer sort_order")
    if not isinstance(answer_type, str) or answer_type not in _ANSWER_TYPES:
        raise WorkflowConfigurationError(f"step {step_id} has an unsupported answer_type")
    raw_enum_values = raw.get("enum_values", [])
    if not isinstance(raw_enum_values, list) or any(not isinstance(value, str) for value in raw_enum_values):
        raise WorkflowConfigurationError(f"step {step_id} has malformed enum_values")
    if answer_type == "enum" and raw.get("enabled") is True and (
        not raw_enum_values or any(not value.strip() for value in raw_enum_values)
        or len(set(raw_enum_values)) != len(raw_enum_values)
    ):
        raise WorkflowConfigurationError(f"enum step '{field_key}' needs unique non-empty enum_values")

    phrases = raw.get("localized_phrases", {})
    rules = raw.get("validation_rules", [])
    branches = raw.get("branches", [])
    if not isinstance(phrases, Mapping) or not isinstance(rules, list) or not isinstance(branches, list):
        raise WorkflowConfigurationError(f"step {step_id} has malformed workflow fields")

    frozen_rules: list[ValidationRule] = []
    for rule in rules:
        if not isinstance(rule, Mapping):
            raise WorkflowConfigurationError(f"step {step_id} has a malformed validation rule")
        frozen_rules.append(ValidationRule(str(rule.get("operator", "")), _freeze_json(rule.get("operand"))))

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
                    operator=str(condition.get("operator", "")),
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

    raw_failure_target = raw.get("validation_failure_target")
    failure_target: WorkflowTarget | None = None
    if raw_failure_target is not None:
        if not isinstance(raw_failure_target, Mapping):
            raise WorkflowConfigurationError(f"step {step_id} has a malformed validation failure target")
        target_id = raw_failure_target.get("target_step_id")
        outcome = raw_failure_target.get("outcome")
        has_step = target_id is not None
        has_outcome = outcome is not None
        if has_step == has_outcome:
            raise WorkflowConfigurationError(f"step {step_id} needs exactly one validation failure target")
        failure_target = WorkflowTarget(
            str(target_id) if target_id is not None else None,
            outcome if isinstance(outcome, str) else None,
        )

    return WorkflowStep(
        id=str(step_id),
        field_key=field_key,
        sort_order=sort_order,
        enabled=raw.get("enabled") is True,
        required=raw.get("required") is not False,
        enum_values=tuple(raw_enum_values),
        answer_type=answer_type,
        wording_mode=raw.get("wording_mode") if isinstance(raw.get("wording_mode"), str) else "flexible",
        title=raw.get("title") if isinstance(raw.get("title"), str) else field_key,
        objective=raw.get("objective") if isinstance(raw.get("objective"), str) else "",
        extraction_instructions=(
            raw.get("extraction_instructions")
            if isinstance(raw.get("extraction_instructions"), str)
            else ""
        ),
        clarification_instructions=(
            raw.get("clarification_instructions")
            if isinstance(raw.get("clarification_instructions"), str)
            else ""
        ),
        localized_phrases=tuple(
            (str(language), phrase)
            for language, phrase in phrases.items()
            if isinstance(phrase, str)
        ),
        validation_rules=tuple(frozen_rules),
        branches=tuple(frozen_branches),
        validation_failure_target=failure_target,
    )


def _immutable_answers(values: Mapping[str, JsonScalar]) -> Mapping[str, JsonScalar]:
    return MappingProxyType(dict(values))


def _immutable_openings(values: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(values))


def _step_indexes(state: WorkflowState) -> tuple[dict[str, WorkflowStep], dict[str, WorkflowStep], dict[str, int]]:
    by_id: dict[str, WorkflowStep] = {}
    by_key: dict[str, WorkflowStep] = {}
    position: dict[str, int] = {}
    for index, step in enumerate(state.steps):
        if step.id in by_id:
            raise WorkflowConfigurationError(f"duplicate step id '{step.id}'")
        by_id[step.id] = step
        if step.enabled:
            if step.field_key in by_key:
                raise WorkflowConfigurationError(f"duplicate enabled field_key '{step.field_key}'")
            by_key[step.field_key] = step
            position[step.field_key] = index
    return by_id, by_key, position


def _next_enabled(state: WorkflowState, step_id: str) -> str | None:
    current_index = next((index for index, step in enumerate(state.steps) if step.id == step_id), None)
    if current_index is None:
        raise WorkflowConfigurationError(f"unknown step id '{step_id}'")
    return next(
        (step.id for step in state.steps[current_index + 1 :] if step.enabled),
        None,
    )


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _normalize_value(
    answer_type: str,
    value: JsonScalar,
    enum_values: tuple[str, ...] = (),
) -> tuple[JsonScalar | None, str | None]:
    if answer_type == "boolean":
        return (value, None) if type(value) is bool else (None, "expected a boolean")
    if answer_type == "text":
        return (value, None) if isinstance(value, str) else (None, "expected text")
    if answer_type == "enum":
        if not isinstance(value, str) or value not in enum_values:
            return None, "expected one of the configured enum values"
        return value, None
    if answer_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "expected a finite number"
        if _as_decimal(value) is None:
            return None, "expected a finite number"
        return value, None
    if answer_type == "currency":
        if not isinstance(value, str):
            return None, "expected currency as a decimal string"
        number = _as_decimal(value)
        if number is None:
            return None, "expected currency as a finite decimal string"
        return value.strip(), None
    return None, f"{answer_type} steps do not collect values"


def _event_value(value: JsonScalar) -> JsonScalar:
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return value


def _comparison_value(answer_type: str, value: Any) -> Any:
    if answer_type in {"number", "currency"}:
        return _as_decimal(value)
    return value


def _compare(
    answer_type: str,
    actual: JsonScalar,
    operator: str,
    operand: Any,
) -> bool:
    if operator not in _OPERATORS:
        return False
    left = _comparison_value(answer_type, actual)
    if left is None:
        return False
    operands = operand if operator == "in" else (operand,)
    if operator == "in" and not isinstance(operands, (list, tuple)):
        return False
    right_values = [_comparison_value(answer_type, item) for item in operands]
    if any(right is None for right in right_values):
        return False
    if operator == "eq":
        return left == right_values[0]
    if operator == "ne":
        return left != right_values[0]
    if operator == "in":
        return left in right_values
    if operator in _ORDER_OPERATORS:
        try:
            return {
                "gt": left > right_values[0],
                "gte": left >= right_values[0],
                "lt": left < right_values[0],
                "lte": left <= right_values[0],
            }[operator]
        except TypeError:
            return False
    return False


def _rules_failed(step: WorkflowStep, value: JsonScalar) -> tuple[str, ...]:
    failures: list[str] = []
    for index, rule in enumerate(step.validation_rules, 1):
        if not _compare(step.answer_type, value, rule.operator, rule.operand):
            failures.append(f"rule {index} failed")
    return tuple(failures)


def _condition_matches(
    condition: BranchCondition,
    by_key: Mapping[str, WorkflowStep],
    answers: Mapping[str, JsonScalar],
) -> bool:
    referenced = by_key.get(condition.field_key)
    if referenced is None or condition.field_key not in answers:
        return False
    return _compare(
        referenced.answer_type,
        answers[condition.field_key],
        condition.operator,
        condition.operand,
    )


def _branch_target(
    step: WorkflowStep,
    by_key: Mapping[str, WorkflowStep],
    answers: Mapping[str, JsonScalar],
) -> WorkflowTarget | None:
    for branch in step.branches:
        if all(_condition_matches(condition, by_key, answers) for condition in branch.conditions):
            if (branch.target_step_id is None) == (branch.outcome is None):
                raise WorkflowConfigurationError(f"step {step.id} has a branch without exactly one target")
            return WorkflowTarget(branch.target_step_id, branch.outcome)
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
        if step is None or not step.enabled:
            raise WorkflowConfigurationError(f"workflow target '{target.target_step_id}' is missing or disabled")
        return replace(state, current_step_id=step.id, completed=False, outcome=None)
    if target.outcome is None:
        raise WorkflowConfigurationError("workflow target has no destination")
    _, by_key, _ = _step_indexes(state)
    if target.outcome not in by_key or by_key[target.outcome].answer_type != "outcome":
        raise WorkflowConfigurationError(f"workflow outcome '{target.outcome}' is not configured")
    return _emit_outcome(state, target.outcome, events)


def _action_for_exact(step: WorkflowStep, state: WorkflowState) -> WorkflowAction:
    phrase = step.phrase(state.language)
    if not isinstance(phrase, str) or not phrase.strip():
        raise WorkflowConfigurationError(
            f"exact step '{step.field_key}' has no phrase for language '{state.language}'"
        )
    playback = state.opening_states.get(step.id, "pending")
    return WorkflowAction(
        kind="exact_opening",
        step_id=step.id,
        field_key=step.field_key,
        content=phrase,
        resume=playback == "interrupted",
    )


def _present(state: WorkflowState, events: list[dict[str, Any]]) -> WorkflowTransition:
    by_id, _, _ = _step_indexes(state)
    current_step_id = state.current_step_id
    seen: set[str] = set()

    while current_step_id is not None:
        step = by_id.get(current_step_id)
        if step is None or not step.enabled:
            raise WorkflowConfigurationError(f"current step '{current_step_id}' is missing or disabled")
        if current_step_id in seen:
            raise WorkflowConfigurationError("workflow route contains a cycle")
        seen.add(current_step_id)
        state = replace(state, current_step_id=current_step_id)

        if step.answer_type == "outcome":
            state = _emit_outcome(state, step.field_key, events)
            return WorkflowTransition(state, tuple(events), WorkflowAction(
                kind="outcome", step_id=step.id, field_key=step.field_key, content=step.field_key
            ))

        if step.answer_type == "message":
            if step.id in state.completed_messages:
                current_step_id = _next_enabled(state, step.id)
                state = replace(state, current_step_id=current_step_id)
                continue
            if step.wording_mode == "exact":
                playback = state.opening_states.get(step.id, "pending")
                if playback != "complete":
                    return WorkflowTransition(state, tuple(events), _action_for_exact(step, state))
            state = replace(
                state,
                current_step_id=_next_enabled(state, step.id),
                completed_messages=state.completed_messages | {step.id},
            )
            return WorkflowTransition(
                state,
                tuple(events),
                WorkflowAction(kind="message", step_id=step.id, field_key=step.field_key, content=step.objective),
            )

        if step.wording_mode == "exact" and state.opening_states.get(step.id, "pending") != "complete":
            return WorkflowTransition(state, tuple(events), _action_for_exact(step, state))
        return WorkflowTransition(
            state,
            tuple(events),
            WorkflowAction(kind="ask", step_id=step.id, field_key=step.field_key),
        )

    if not state.completed:
        state = replace(state, current_step_id=None, completed=True)
        return WorkflowTransition(state, tuple(events), WorkflowAction(kind="complete"))
    return WorkflowTransition(state, tuple(events), None)


def start_workflow(step_snapshot: Sequence[Mapping[str, Any]], language: str) -> WorkflowTransition:
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
        current_step_id=next((step.id for step in ordered if step.enabled), None),
        opening_states=_immutable_openings(
            {
                step.id: "pending"
                for step in ordered
                if step.enabled and step.wording_mode == "exact" and step.answer_type != "outcome"
            }
        ),
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

    _, by_key, positions = _step_indexes(state)
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
    requested_step_id = state.current_step_id
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
        if step is None or not step.enabled:
            raise WorkflowConfigurationError(f"current step '{step_id}' is missing or disabled")
        if step.answer_type in {"message", "outcome"}:
            return _present(state, events)

        extraction = candidates.get(step.field_key)
        pending_correction = step.field_key in state.pending_corrections
        if extraction is None or extraction.status == "missing":
            if step.field_key in state.answers and not pending_correction:
                target = _branch_target(step, by_key, state.answers)
                state = _route_to_target(state, target, events) if target else replace(
                    state, current_step_id=_next_enabled(state, step.id)
                )
                continue
            if not step.required and not pending_correction:
                state = replace(state, current_step_id=_next_enabled(state, step.id))
                continue
            if step.id != requested_step_id and not pending_correction:
                return _present(state, events)
            reason = "missing"
            return WorkflowTransition(
                state,
                tuple(events),
                WorkflowAction(
                    kind="clarify",
                    step_id=step.id,
                    field_key=step.field_key,
                    content=step.clarification_instructions,
                    reason=reason,
                ),
            )

        if extraction.status == "ambiguous":
            if extraction.is_correction or pending_correction:
                state = replace(
                    state,
                    pending_corrections=state.pending_corrections | {step.field_key},
                )
            return WorkflowTransition(
                state,
                tuple(events),
                WorkflowAction(
                    kind="clarify",
                    step_id=step.id,
                    field_key=step.field_key,
                    content=step.clarification_instructions,
                    reason="ambiguous",
                ),
            )

        if step.field_key in state.answers and not (extraction.is_correction or pending_correction):
            target = _branch_target(step, by_key, state.answers)
            state = _route_to_target(state, target, events) if target else replace(
                state, current_step_id=_next_enabled(state, step.id)
            )
            continue

        candidate, problem = _normalize_value(step.answer_type, extraction.value, step.enum_values)
        if problem is not None:
            events.append(
                {
                    "type": "answer",
                    "step_id": step.id,
                    "field_key": step.field_key,
                    "value": _event_value(extraction.value),
                    "accepted": False,
                    "reason": "invalid_type",
                    "validation_errors": [problem],
                }
            )
            if extraction.is_correction or pending_correction:
                state = replace(
                    state,
                    pending_corrections=state.pending_corrections | {step.field_key},
                )
            return WorkflowTransition(
                state,
                tuple(events),
                WorkflowAction(
                    kind="clarify",
                    step_id=step.id,
                    field_key=step.field_key,
                    content=step.clarification_instructions,
                    reason="invalid_type",
                ),
            )

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

        failures = _rules_failed(step, candidate)
        if failures:
            answers = dict(state.answers)
            answers.pop(step.field_key, None)
            state = replace(state, answers=_immutable_answers(answers))
            events.append(
                {
                    "type": "answer",
                    "step_id": step.id,
                    "field_key": step.field_key,
                    "value": candidate,
                    "accepted": False,
                    "reason": "validation_failed",
                    "validation_errors": list(failures),
                }
            )
            if step.validation_failure_target is None:
                raise WorkflowConfigurationError(
                    f"step '{step.field_key}' has validation rules but no failure target"
                )
            state = _route_to_target(state, step.validation_failure_target, events)
            if state.outcome is not None:
                action = WorkflowAction(
                    kind="outcome", field_key=state.outcome, content=state.outcome
                )
                return WorkflowTransition(state, tuple(events), action)
            continue

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
            state, current_step_id=_next_enabled(state, step.id)
        )
        if state.outcome is not None:
            return WorkflowTransition(
                state,
                tuple(events),
                WorkflowAction(kind="outcome", field_key=state.outcome, content=state.outcome),
            )

    return _present(state, events)


def mark_output_started(state: WorkflowState, step_id: str) -> WorkflowTransition:
    step = next((item for item in state.steps if item.id == step_id), None)
    if step is None or step.wording_mode != "exact" or step.answer_type == "outcome":
        raise WorkflowConfigurationError(f"step '{step_id}' has no exact opening to play")
    if state.opening_states.get(step_id, "pending") == "complete":
        raise WorkflowConfigurationError(f"step '{step_id}' exact opening is already complete")
    openings = dict(state.opening_states)
    openings[step_id] = "playing"
    return WorkflowTransition(replace(state, opening_states=_immutable_openings(openings)))


def mark_output_completed(state: WorkflowState, step_id: str) -> WorkflowTransition:
    step = next((item for item in state.steps if item.id == step_id), None)
    if step is None or step.wording_mode != "exact" or step.answer_type == "outcome":
        raise WorkflowConfigurationError(f"step '{step_id}' has no exact opening to complete")
    if state.opening_states.get(step_id) != "playing":
        raise WorkflowConfigurationError(f"step '{step_id}' output was not playing")
    openings = dict(state.opening_states)
    openings[step_id] = "complete"
    state = replace(state, opening_states=_immutable_openings(openings))
    if step.answer_type == "message" and state.current_step_id == step_id:
        state = replace(
            state,
            current_step_id=_next_enabled(state, step_id),
            completed_messages=state.completed_messages | {step_id},
        )
        return _present(state, [])
    return WorkflowTransition(state)


def mark_output_interrupted(state: WorkflowState, step_id: str) -> WorkflowTransition:
    step = next((item for item in state.steps if item.id == step_id), None)
    if step is None or step.wording_mode != "exact" or step.answer_type == "outcome":
        raise WorkflowConfigurationError(f"step '{step_id}' has no exact opening to interrupt")
    if state.opening_states.get(step_id) == "complete":
        return WorkflowTransition(state)
    openings = dict(state.opening_states)
    openings[step_id] = "interrupted"
    state = replace(state, opening_states=_immutable_openings(openings))
    action = _action_for_exact(step, state) if state.current_step_id == step_id else None
    return WorkflowTransition(state, action=action)
