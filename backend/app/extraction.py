from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Mapping, Protocol, Sequence, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from .workflow import FieldExtraction


AnswerType = Literal["boolean", "text", "number", "currency", "enum"]
ConversationRole = Literal["user", "assistant"]


class ExtractionField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field_key: str = Field(min_length=1, max_length=128)
    answer_type: AnswerType
    enum_values: tuple[str, ...] = ()
    enabled: bool = True

    @model_validator(mode="after")
    def enum_values_only_for_enum(self) -> ExtractionField:
        if self.answer_type != "enum" and self.enum_values:
            raise ValueError("enum_values are only valid for enum fields")
        return self


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ConversationRole
    content: str


class FrozenPromptInstructions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    global_policy: str
    agent_instructions: str


class PromptStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field_key: str | None
    title: str
    objective: str
    required: bool = True
    answer_type: str | None = None
    extraction_instructions: str = ""
    clarification_instructions: str = ""
    wording_mode: Literal["exact", "flexible"] = "flexible"
    localized_phrases: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class PromptConfigurationError(ValueError):
    pass


class ExtractionError(RuntimeError):
    pass


class _Message(TypedDict):
    role: Literal["system", "developer", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class TurnMessages:
    messages: tuple[_Message, ...]
    direct_tts_text: str | None = None


class _ChatCompletions(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


class _Chat(Protocol):
    completions: _ChatCompletions


class OpenAIClient(Protocol):
    """The chat interface exposed by a backend-keyed AsyncOpenAI client."""

    chat: _Chat


_EXTRACTION_INSTRUCTIONS = (
    "Extract only values explicitly supplied by the caller for the field keys in the input. "
    "Return every supplied field exactly once. Use value only when one value is clear, "
    "ambiguous when the caller supplied conflicting or unclear candidates, and missing "
    "when the recent conversation supplies no new value. Preserve the candidate as heard; "
    "do not normalize it or decide whether it passes validation. Mark is_correction true "
    "only when the caller explicitly revises a confirmed answer. Never invent an answer "
    "for a field that was not supplied."
)


def _schema(field_keys: Sequence[str]) -> dict[str, Any]:
    answer = {
        "type": "object",
        "properties": {
            "field_key": {"type": "string", "enum": list(field_keys)},
            "status": {"type": "string", "enum": ["value", "missing", "ambiguous"]},
            "value": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "number"},
                    {"type": "boolean"},
                    {"type": "null"},
                ]
            },
            "is_correction": {"type": "boolean"},
        },
        "required": ["field_key", "status", "value", "is_correction"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "caller_field_extraction",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"answers": {"type": "array", "items": answer}},
                "required": ["answers"],
                "additionalProperties": False,
            },
        },
    }


def _field_data(field: ExtractionField) -> dict[str, Any]:
    return {
        "field_key": field.field_key,
        "answer_type": field.answer_type,
        "enum_values": list(field.enum_values),
    }


def _json_load(content: str) -> Any:
    def reject_constant(_: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(content, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError, TypeError) as error:
        raise ExtractionError("OpenAI returned invalid structured extraction data") from error


def _eligible_fields(
    unanswered_fields: Sequence[ExtractionField],
    correction_fields: Sequence[ExtractionField],
    confirmed_answers: Mapping[str, Any],
) -> tuple[list[ExtractionField], list[ExtractionField], dict[str, Any]]:
    unanswered = [field for field in unanswered_fields if field.enabled]
    corrections = [field for field in correction_fields if field.enabled]
    keys = [field.field_key for field in (*unanswered, *corrections)]
    if len(keys) != len(set(keys)):
        raise ValueError("field keys must be unique across unanswered and correction fields")

    missing_confirmations = [field.field_key for field in corrections if field.field_key not in confirmed_answers]
    if missing_confirmations:
        raise ValueError("every correction field must have a confirmed answer")

    eligible_confirmed = {field.field_key: confirmed_answers[field.field_key] for field in corrections}
    try:
        json.dumps(eligible_confirmed, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("confirmed answers must be JSON-compatible values") from error
    return unanswered, corrections, eligible_confirmed


async def extract_answers(
    client: OpenAIClient,
    model: str,
    unanswered_fields: Sequence[ExtractionField],
    recent_conversation: Sequence[ConversationTurn],
    *,
    correction_fields: Sequence[ExtractionField] = (),
    confirmed_answers: Mapping[str, Any] | None = None,
) -> dict[str, FieldExtraction]:
    """Extract field candidates through a backend-keyed OpenAI chat client.

    The caller supplies only enabled unanswered fields and confirmed fields that
    may be corrected. No question titles, objectives, validation rules, or other
    step prompts cross this extraction boundary.
    """
    unanswered, corrections, previous = _eligible_fields(
        unanswered_fields,
        correction_fields,
        confirmed_answers or {},
    )
    fields = [*unanswered, *corrections]
    if not fields:
        return {}
    if not model:
        raise ValueError("an OpenAI model is required")

    field_keys = [field.field_key for field in fields]
    user_payload = {
        "unanswered_fields": [_field_data(field) for field in unanswered],
        "correction_fields": [_field_data(field) for field in corrections],
        "confirmed_answers": previous,
        "recent_conversation": [turn.model_dump() for turn in recent_conversation],
    }
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": _EXTRACTION_INSTRUCTIONS},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, allow_nan=False)},
        ],
        "response_format": _schema(field_keys),
        "temperature": 0,
    }
    response = await client.chat.completions.create(**request)
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as error:
        raise ExtractionError("OpenAI returned no structured extraction message") from error
    if not isinstance(content, str):
        raise ExtractionError("OpenAI returned no structured extraction message")

    payload = _json_load(content)
    if not isinstance(payload, dict) or set(payload) != {"answers"} or not isinstance(payload["answers"], list):
        raise ExtractionError("OpenAI returned an invalid extraction shape")

    from .workflow import FieldExtraction

    results: dict[str, FieldExtraction] = {}
    allowed = set(field_keys)
    correction_keys = {field.field_key for field in corrections}
    for answer in payload["answers"]:
        if not isinstance(answer, dict) or set(answer) != {"field_key", "status", "value", "is_correction"}:
            raise ExtractionError("OpenAI returned an invalid extraction item")
        key = answer["field_key"]
        status = answer["status"]
        value = answer["value"]
        is_correction = answer["is_correction"]
        if not isinstance(key, str) or key not in allowed or key in results:
            raise ExtractionError("OpenAI returned an unknown or duplicate field key")
        if not isinstance(status, str) or status not in {"value", "missing", "ambiguous"}:
            raise ExtractionError("OpenAI returned an unknown extraction status")
        if type(is_correction) is not bool:
            raise ExtractionError("OpenAI returned an invalid correction marker")
        if status == "value":
            if value is None or type(value) not in {str, int, float, bool}:
                raise ExtractionError("OpenAI returned an invalid candidate value")
            if isinstance(value, float) and not math.isfinite(value):
                raise ExtractionError("OpenAI returned an invalid candidate value")
        elif value is not None:
            raise ExtractionError("OpenAI returned a value for a non-value status")
        if is_correction and (key not in correction_keys or status == "missing"):
            raise ExtractionError("OpenAI returned an invalid correction marker")
        if key in correction_keys and status == "value" and not is_correction:
            raise ExtractionError("OpenAI did not identify a corrected confirmed answer")
        try:
            results[key] = FieldExtraction(
                status=status,
                value=value,
                is_correction=is_correction,
            )
        except (TypeError, ValueError) as error:
            raise ExtractionError("OpenAI returned an invalid extraction result") from error

    if set(results) != allowed:
        raise ExtractionError("OpenAI omitted one or more eligible fields")
    return results


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PromptConfigurationError("turn context must contain JSON-compatible values") from error


def build_turn_messages(
    *,
    instructions: FrozenPromptInstructions,
    active_step: PromptStep | None,
    recent_conversation: Sequence[ConversationTurn],
    confirmed_answers: Mapping[str, Any],
    validation_feedback: Sequence[str],
    language: str,
    exact_opening_pending: bool = False,
) -> TurnMessages:
    """Build priority-separated messages from frozen instructions and current turn state."""
    if not language:
        raise PromptConfigurationError("a call language is required")
    if active_step is not None and not active_step.enabled:
        raise PromptConfigurationError("a disabled step cannot be used for a turn")

    if exact_opening_pending:
        if active_step is None or active_step.wording_mode != "exact":
            raise PromptConfigurationError("an exact opening requires an active exact-wording step")
        phrase = active_step.localized_phrases.get(language)
        if not phrase:
            raise PromptConfigurationError("the exact opening is missing for the call language")
        return TurnMessages(messages=(), direct_tts_text=phrase)

    messages: list[_Message] = [
        {"role": "system", "content": instructions.global_policy},
        {"role": "developer", "content": instructions.agent_instructions},
    ]
    if active_step is not None:
        step_instructions = {
            "field_key": active_step.field_key,
            "title": active_step.title,
            "objective": active_step.objective,
            "required": active_step.required,
            "answer_type": active_step.answer_type,
            "extraction_instructions": active_step.extraction_instructions,
            "clarification_instructions": active_step.clarification_instructions,
        }
        messages.append(
            {"role": "developer", "content": _json_text({"active_step": step_instructions})}
        )
    turn_context = {
        "language": language,
        "confirmed_answers": confirmed_answers,
        "validation_feedback": list(validation_feedback),
    }
    messages.append({"role": "developer", "content": _json_text(turn_context)})
    messages.extend(turn.model_dump() for turn in recent_conversation)
    return TurnMessages(messages=tuple(messages))
