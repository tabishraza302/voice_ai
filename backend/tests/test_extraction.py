import json
from types import SimpleNamespace

import pytest

from app.extraction import (
    ConversationTurn,
    ExtractionError,
    ExtractionField,
    FrozenPromptInstructions,
    PromptConfigurationError,
    PromptStep,
    build_turn_messages,
    extract_answers,
)


class DeterministicCompletions:
    def __init__(self, content):
        self.content = content
        self.requests = []

    async def create(self, **request):
        self.requests.append(request)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


def model_client(content):
    completions = DeterministicCompletions(content)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


@pytest.mark.anyio
async def test_extraction_distinguishes_values_missing_ambiguity_and_corrections():
    response = {
        "answers": [
            {"field_key": "age", "status": "ambiguous", "value": None, "is_correction": False},
            {"field_key": "is_salaried", "status": "value", "value": "yes", "is_correction": False},
            {
                "field_key": "monthly_salary",
                "status": "value",
                "value": "24000",
                "is_correction": True,
            },
            {"field_key": "employer", "status": "missing", "value": None, "is_correction": False},
        ]
    }
    client, completions = model_client(json.dumps(response))
    result = await extract_answers(
        client,
        "gpt-4o-mini",
        unanswered_fields=[
            ExtractionField(field_key="age"),
            ExtractionField(field_key="is_salaried"),
            ExtractionField(field_key="employer"),
        ],
        recent_conversation=[
            ConversationTurn(role="user", content="I am either twenty-four or twenty-five."),
            ConversationTurn(role="assistant", content="You said twenty-four."),
            ConversationTurn(role="user", content="Actually, my monthly salary is 24000."),
        ],
        correction_fields=[ExtractionField(field_key="monthly_salary")],
        confirmed_answers={"monthly_salary": "21000", "unrelated_answer": "private"},
    )

    assert set(result) == {"age", "is_salaried", "monthly_salary", "employer"}
    assert result["age"].status == "ambiguous"
    assert result["age"].value is None
    assert result["is_salaried"].status == "value"
    assert result["is_salaried"].value == "yes"
    assert result["monthly_salary"].status == "value"
    assert result["monthly_salary"].value == "24000"
    assert result["monthly_salary"].is_correction is True
    assert result["employer"].status == "missing"
    assert result["employer"].value is None

    request = completions.requests[0]
    assert request["model"] == "gpt-4o-mini"
    assert request["temperature"] == 0
    assert request["response_format"]["json_schema"]["strict"] is True
    payload = json.loads(request["messages"][1]["content"])
    assert payload["unanswered_fields"] == [
        {"field_key": "age"},
        {"field_key": "is_salaried"},
        {"field_key": "employer"},
    ]
    assert payload["correction_fields"] == [
        {"field_key": "monthly_salary"}
    ]
    assert payload["confirmed_answers"] == {"monthly_salary": "21000"}
    assert "unrelated_answer" not in request["messages"][1]["content"]
    assert "unrelated_answer" not in request["messages"][1]["content"]
    assert "full unrelated prompt" not in request["messages"][1]["content"]


@pytest.mark.anyio
async def test_extraction_skips_model_when_no_fields_remain():
    client, completions = model_client('{"answers": []}')

    result = await extract_answers(
        client,
        "gpt-4o-mini",
        unanswered_fields=[],
        recent_conversation=[ConversationTurn(role="user", content="Thanks, goodbye.")],
    )

    assert result == {}
    assert completions.requests == []

@pytest.mark.anyio
async def test_extraction_rejects_a_response_that_omits_an_eligible_field():
    client, _ = model_client(
        json.dumps(
            {
                "answers": [
                    {"field_key": "age", "status": "missing", "value": None, "is_correction": False}
                ]
            }
        )
    )

    with pytest.raises(ExtractionError):
        await extract_answers(
            client,
            "gpt-4o-mini",
            unanswered_fields=[
                ExtractionField(field_key="age"),
                ExtractionField(field_key="city"),
            ],
            recent_conversation=[ConversationTurn(role="user", content="I am 27.")],
        )


def test_turn_messages_keep_policy_and_agent_priority_and_only_include_the_active_step():
    built = build_turn_messages(
        instructions=FrozenPromptInstructions(
            global_policy="Frozen policy text.",
            agent_instructions="Agent-level instructions.",
        ),
        active_step=PromptStep(
            field_key="age",
            title="Age",
            objective="Collect the caller's age.",
        ),
        recent_conversation=[
            ConversationTurn(role="user", content="I am 27."),
            ConversationTurn(role="assistant", content="Thank you."),
        ],
        confirmed_answers={"is_salaried": True},
        validation_feedback=["Age must be over 24."],
        language="en",
    )

    assert [message["role"] for message in built.messages] == [
        "system",
        "developer",
        "developer",
        "developer",
        "user",
        "assistant",
    ]
    assert built.messages[0]["content"] == "Frozen policy text."
    assert built.messages[1]["content"] == "Agent-level instructions."
    assert "Collect the caller's age." in built.messages[2]["content"]
    assert "Age" in built.messages[2]["content"]
    turn_context = json.loads(built.messages[3]["content"])
    assert turn_context == {
        "language": "en",
        "confirmed_answers": {"is_salaried": True},
        "validation_feedback": ["Age must be over 24."],
    }
    assert [message["content"] for message in built.messages[-2:]] == [
        "I am 27.",
        "Thank you.",
    ]
