import os
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test")
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

from app.agents import Agent
from app.db import Base, Session, engine
from app.main import app
from app.models import User
from app.steps import Step, ValidationFailureTarget, validate_publish


@pytest.fixture
async def client():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000") as test_client:
        yield test_client


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def registered_client(client, email=None):
    email = email or f"{uuid.uuid4()}@example.com"
    password = "correct horse battery staple"
    assert (await client.post("/auth/register", json={"email": email, "password": password})).status_code == 201
    login = await client.post("/auth/login", json={"email": email, "password": password})
    return login.json()["csrf_token"]


async def create_agent(email):
    async with Session() as db:
        user = await db.scalar(select(User).where(User.email == email))
        agent = Agent(owner_id=user.id, slug=f"agent-{uuid.uuid4().hex}", name="Support")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id


def mutation_headers(csrf_token):
    return {"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token}


def question(field_key, *, title="Question", answer_type="text", wording_mode="flexible"):
    return {
        "field_key": field_key,
        "title": title,
        "objective": "Collect the caller's answer.",
        "required": True,
        "enabled": True,
        "answer_type": answer_type,
        "wording_mode": wording_mode,
        "localized_phrases": {},
        "extraction_instructions": "Extract the value from the caller.",
        "clarification_instructions": "Ask for a clearer answer.",
        "validation_rules": [],
        "branches": [],
    }


@pytest.mark.anyio
async def test_step_routes_require_csrf_and_hide_another_owners_steps(client):
    owner_email = f"{uuid.uuid4()}@example.com"
    owner_csrf = await registered_client(client, owner_email)
    agent_id = await create_agent(owner_email)
    created = await client.post(
        f"/agents/{agent_id}/steps",
        json=question("caller_name"),
        headers=mutation_headers(owner_csrf),
    )
    assert created.status_code == 201
    step_id = created.json()["id"]

    assert (await client.post(f"/agents/{agent_id}/steps", json=question("another"))).status_code == 403

    other = AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000")
    try:
        other_email = f"{uuid.uuid4()}@example.com"
        other_csrf = await registered_client(other, other_email)
        assert (await other.get(f"/agents/{agent_id}/steps")).status_code == 404
        assert (await other.patch(
            f"/agents/{agent_id}/steps/{step_id}",
            json={"title": "Changed"},
            headers=mutation_headers(other_csrf),
        )).status_code == 404
        assert (await other.delete(
            f"/agents/{agent_id}/steps/{step_id}",
            headers=mutation_headers(other_csrf),
        )).status_code == 404
        assert (await other.put(
            f"/agents/{agent_id}/steps/order",
            json={"step_ids": [step_id]},
            headers=mutation_headers(other_csrf),
        )).status_code == 404
    finally:
        await other.aclose()


@pytest.mark.anyio
async def test_duplicate_field_keys_are_rejected_with_conflict(client):
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    headers = mutation_headers(csrf_token)

    first = await client.post(f"/agents/{agent_id}/steps", json=question("caller_name"), headers=headers)
    duplicate = await client.post(f"/agents/{agent_id}/steps", json=question("caller_name"), headers=headers)

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert len((await client.get(f"/agents/{agent_id}/steps")).json()) == 1


@pytest.mark.anyio
async def test_reordering_requires_each_step_once_and_keeps_order_on_invalid_request(client):
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    headers = mutation_headers(csrf_token)
    ids = []
    for key in ("first", "second", "third"):
        response = await client.post(f"/agents/{agent_id}/steps", json=question(key), headers=headers)
        assert response.status_code == 201
        ids.append(response.json()["id"])

    ordered = await client.put(
        f"/agents/{agent_id}/steps/order",
        json={"step_ids": [ids[2], ids[0], ids[1]]},
        headers=headers,
    )
    assert ordered.status_code == 200
    assert [step["id"] for step in ordered.json()] == [ids[2], ids[0], ids[1]]

    invalid = await client.put(
        f"/agents/{agent_id}/steps/order",
        json={"step_ids": [ids[2], ids[2], ids[1]]},
        headers=headers,
    )
    assert invalid.status_code == 422
    current = await client.get(f"/agents/{agent_id}/steps")
    assert [step["id"] for step in current.json()] == [ids[2], ids[0], ids[1]]


@pytest.mark.anyio
async def test_incomplete_draft_step_can_be_saved(client):
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    draft = question("draft", title="")
    draft["answer_type"] = None
    draft["wording_mode"] = None

    response = await client.post(
        f"/agents/{agent_id}/steps",
        json=draft,
        headers=mutation_headers(csrf_token),
    )

    assert response.status_code == 201
    assert response.json()["title"] == ""
    assert response.json()["answer_type"] is None


def make_step(
    agent_id,
    field_key,
    *,
    answer_type="text",
    rules=None,
    branches=None,
    enum_values=None,
    validation_failure_target=None,
    enabled=True,
    wording_mode="flexible",
    phrases=None,
    sort_order=0,
):
    return Step(
        id=uuid.uuid4(),
        agent_id=agent_id,
        field_key=field_key,
        sort_order=sort_order,
        title=field_key.title(),
        objective="Collect the caller's answer.",
        required=True,
        enabled=enabled,
        answer_type=answer_type,
        wording_mode=wording_mode,
        localized_phrases=phrases or {},
        extraction_instructions="Extract the value from the caller.",
        clarification_instructions="Ask for a clearer answer.",
        validation_rules=rules or [],
        branches=branches or [],
        enum_values=enum_values or [],
        validation_failure_target=validation_failure_target,
    )


def test_publish_validation_reports_missing_exact_phrases_and_invalid_typed_rules():
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(languages=["en", "hi"])
    step = make_step(
        agent_id,
        "is_employed",
        answer_type="boolean",
        wording_mode="exact",
        phrases={"en": "Are you employed?"},
        rules=[
            {"operator": "contains", "operand": "yes"},
            {"operator": "eq", "operand": "true"},
        ],
    )

    errors = validate_publish(agent, [step])

    assert f"steps.{step.id}.localized_phrases" in errors
    assert f"steps.{step.id}.validation_rules" in errors
    assert errors[f"steps.{step.id}.validation_rules"]


def test_publish_validation_reports_missing_and_disabled_branch_targets():
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(languages=["en"])
    source = make_step(agent_id, "source", sort_order=0)
    missing_id = uuid.uuid4()
    disabled = make_step(agent_id, "disabled", enabled=False, sort_order=2)
    router = make_step(
        agent_id,
        "router",
        sort_order=1,
        branches=[
            {"conditions": [{"field_key": "source", "operator": "eq", "operand": "yes"}], "target_step_id": str(missing_id)},
            {"conditions": [{"field_key": "source", "operator": "eq", "operand": "no"}], "target_step_id": str(disabled.id)},
        ],
    )

    errors = validate_publish(agent, [source, router, disabled])

    assert f"steps.{router.id}.branches" in errors
    assert len(errors[f"steps.{router.id}.branches"]) >= 2


def test_publish_validation_rejects_cycles_in_ordered_branch_graph():
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(languages=["en"])
    first = make_step(agent_id, "first", sort_order=0)
    second = make_step(agent_id, "second", sort_order=1)
    first.branches = [{
        "conditions": [{"field_key": "second", "operator": "eq", "operand": "yes"}],
        "target_step_id": str(second.id),
    }]
    second.branches = [{
        "conditions": [{"field_key": "first", "operator": "eq", "operand": "yes"}],
        "target_step_id": str(first.id),
    }]

    errors = validate_publish(agent, [first, second])

    assert any(path.endswith(".branches") for path in errors)


@pytest.mark.anyio
async def test_owner_can_patch_and_delete_a_step(client):
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    headers = mutation_headers(csrf_token)
    created = await client.post(f"/agents/{agent_id}/steps", json=question("caller_name"), headers=headers)
    step_id = created.json()["id"]

    patched = await client.patch(
        f"/agents/{agent_id}/steps/{step_id}",
        json={"title": "Caller name"},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Caller name"

    deleted = await client.delete(f"/agents/{agent_id}/steps/{step_id}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(f"/agents/{agent_id}/steps")).json() == []


@pytest.mark.anyio
async def test_publish_route_returns_step_field_errors(client):
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    draft = question("draft", title="")
    draft["answer_type"] = None
    draft["wording_mode"] = None
    created = await client.post(
        f"/agents/{agent_id}/steps",
        json=draft,
        headers=mutation_headers(csrf_token),
    )

    response = await client.post(
        f"/agents/{agent_id}/publish",
        headers=mutation_headers(csrf_token),
    )

    assert response.status_code == 422
    assert f"steps.{created.json()['id']}.title" in response.json()["detail"]


def test_publish_validation_accepts_a_configured_outcome_branch():
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(languages=["en"])
    answer = make_step(
        agent_id,
        "answer",
        sort_order=0,
        wording_mode="exact",
        phrases={"en": "Do you agree?"},
        branches=[{
            "conditions": [{"field_key": "answer", "operator": "eq", "operand": "yes"}],
            "outcome": "accepted",
        }],
    )
    outcome = make_step(agent_id, "accepted", answer_type="outcome", sort_order=1)

    assert validate_publish(agent, [answer, outcome]) == {}


def test_publish_requires_and_validates_validation_failure_targets():
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(languages=["en"])
    rules = [{"operator": "gt", "operand": 24}]
    missing = make_step(agent_id, "missing", answer_type="number", rules=rules, sort_order=0)
    invalid_id = make_step(
        agent_id,
        "invalid_id",
        answer_type="number",
        rules=rules,
        validation_failure_target={"target_step_id": str(uuid.uuid4()), "outcome": None},
        sort_order=1,
    )
    disabled = make_step(
        agent_id,
        "disabled",
        enabled=False,
        sort_order=2,
    )
    disabled_target = make_step(
        agent_id,
        "disabled_target",
        answer_type="number",
        rules=rules,
        validation_failure_target={"target_step_id": str(disabled.id), "outcome": None},
        sort_order=3,
    )
    valid = make_step(
        agent_id,
        "valid",
        answer_type="number",
        rules=rules,
        validation_failure_target={"target_step_id": None, "outcome": "rejected"},
        sort_order=4,
    )
    rejection = make_step(agent_id, "rejected", answer_type="outcome", sort_order=5)

    errors = validate_publish(agent, [missing, invalid_id, disabled, disabled_target, valid, rejection])

    assert f"steps.{missing.id}.validation_failure_target" in errors
    assert f"steps.{invalid_id.id}.validation_failure_target" in errors
    assert f"steps.{disabled_target.id}.validation_failure_target" in errors
    assert f"steps.{valid.id}.validation_failure_target" not in errors


def test_publish_validation_includes_failure_step_targets_in_cycle_detection():
    agent_id = uuid.uuid4()
    first = make_step(
        agent_id,
        "first",
        answer_type="number",
        rules=[{"operator": "gt", "operand": 0}],
        branches=[{"conditions": [], "target_step_id": None, "outcome": "accepted"}],
        validation_failure_target={"target_step_id": None, "outcome": None},
        sort_order=0,
    )
    second = make_step(
        agent_id,
        "second",
        answer_type="number",
        rules=[{"operator": "gt", "operand": 0}],
        branches=[{"conditions": [], "target_step_id": None, "outcome": "accepted"}],
        validation_failure_target={"target_step_id": str(first.id), "outcome": None},
        sort_order=1,
    )
    first.validation_failure_target = {"target_step_id": str(second.id), "outcome": None}
    agent = SimpleNamespace(languages=["en"])
    accepted = make_step(agent_id, "accepted", answer_type="outcome", sort_order=2)

    errors = validate_publish(agent, [first, second, accepted])

    assert f"steps.{first.id}.branches" in errors


def test_validation_failure_target_requires_exactly_one_destination():
    with pytest.raises(ValueError):
        ValidationFailureTarget(target_step_id=None, outcome=None)
    with pytest.raises(ValueError):
        ValidationFailureTarget(target_step_id=uuid.uuid4(), outcome="rejected")



def test_publish_validation_requires_nonempty_unique_enum_values():
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(languages=["en"])
    empty = make_step(agent_id, "empty", answer_type="enum", sort_order=0)
    duplicate = make_step(
        agent_id,
        "duplicate",
        answer_type="enum",
        enum_values=["red", "red"],
        rules=[{"operator": "eq", "operand": "blue"}],
        sort_order=1,
    )

    errors = validate_publish(agent, [empty, duplicate])

    assert f"steps.{empty.id}.enum_values" in errors
    assert f"steps.{duplicate.id}.enum_values" in errors
    assert f"steps.{duplicate.id}.validation_rules" in errors


def test_publish_validation_accepts_configured_enum_rule_and_branch_values():
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(languages=["en"])
    choice = make_step(
        agent_id,
        "choice",
        answer_type="enum",
        enum_values=["yes", "no"],
        rules=[{"operator": "in", "operand": ["yes", "no"]}],
        branches=[{
            "conditions": [{"field_key": "choice", "operator": "eq", "operand": "yes"}],
            "outcome": "accepted",
        }],
        sort_order=0,
        validation_failure_target={"target_step_id": None, "outcome": "accepted"},
    )
    accepted = make_step(agent_id, "accepted", answer_type="outcome", sort_order=1)

    assert validate_publish(agent, [choice, accepted]) == {}

def test_publish_validation_rejects_unknown_types_and_non_finite_or_wrong_currency_operands():
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(languages=["en"])
    invalid_type = make_step(agent_id, "unsupported", answer_type="other", sort_order=0)
    number = make_step(
        agent_id,
        "number",
        answer_type="number",
        rules=[{"operator": "gt", "operand": float("inf")}],
        sort_order=1,
    )
    currency = make_step(
        agent_id,
        "currency",
        answer_type="currency",
        rules=[{"operator": "gt", "operand": 100}],
        sort_order=2,
    )

    errors = validate_publish(agent, [invalid_type, number, currency])

    assert f"steps.{invalid_type.id}.answer_type" in errors
    assert f"steps.{number.id}.validation_rules" in errors
    assert f"steps.{currency.id}.validation_rules" in errors


async def published_agent_with_step(client, monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4o-mini")
    monkeypatch.setenv("CARTESIA_API_KEY", "test-only")
    monkeypatch.setenv(
        "VERIFIED_TTS_VOICE_CATALOG",
        '{"cartesia":{"sonic-3.6":{"test-voice":["en"]}}}',
    )
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    headers = mutation_headers(csrf_token)
    step = await client.post(
        f"/agents/{agent_id}/steps",
        json=question("answer"),
        headers=headers,
    )
    assert step.status_code == 201
    updated = await client.patch(
        f"/agents/{agent_id}",
        json={
            "instructions": "Help callers find the right team.",
            "languages": ["en"],
            "default_language": "en",
            "stt_provider": "deepgram",
            "stt_model": "nova-3",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "tts_provider": "cartesia",
            "tts_model": "sonic-3.6",
            "voice_id": "test-voice",
        },
        headers=headers,
    )
    assert updated.status_code == 200
    published = await client.post(f"/agents/{agent_id}/publish", headers=headers)
    assert published.status_code == 200
    return agent_id, step.json()["id"], headers


@pytest.mark.anyio
async def test_published_agent_rejects_invalid_step_create_patch_and_delete(client, monkeypatch):
    agent_id, step_id, headers = await published_agent_with_step(client, monkeypatch)

    patched = await client.patch(
        f"/agents/{agent_id}/steps/{step_id}",
        json={"wording_mode": "exact", "localized_phrases": {}},
        headers=headers,
    )
    assert patched.status_code == 422
    assert f"steps.{step_id}.localized_phrases" in patched.json()["detail"]
    steps = (await client.get(f"/agents/{agent_id}/steps")).json()
    assert [(step["id"], step["wording_mode"]) for step in steps] == [(step_id, "flexible")]

    invalid = question("number_question", answer_type="number")
    invalid["extraction_instructions"] = ""
    created = await client.post(f"/agents/{agent_id}/steps", json=invalid, headers=headers)
    assert created.status_code == 422
    assert any(path.endswith(".extraction_instructions") for path in created.json()["detail"])
    assert [step["id"] for step in (await client.get(f"/agents/{agent_id}/steps")).json()] == [step_id]

    deleted = await client.delete(f"/agents/{agent_id}/steps/{step_id}", headers=headers)
    assert deleted.status_code == 422
    assert [step["id"] for step in (await client.get(f"/agents/{agent_id}/steps")).json()] == [step_id]


@pytest.mark.anyio
async def test_published_agent_accepts_a_disabled_question_until_it_is_enabled(client, monkeypatch):
    agent_id, first_id, headers = await published_agent_with_step(client, monkeypatch)
    payload = question("later")
    payload.update(
        title="New question",
        objective="",
        extraction_instructions="",
        clarification_instructions="",
        enabled=False,
    )

    created = await client.post(f"/agents/{agent_id}/steps", json=payload, headers=headers)

    assert created.status_code == 201
    assert created.json()["enabled"] is False
    assert created.json()["sort_order"] == 1
    step_id = created.json()["id"]

    invalid = await client.patch(
        f"/agents/{agent_id}/steps/{step_id}",
        json={"enabled": True},
        headers=headers,
    )
    assert invalid.status_code == 422
    assert f"steps.{step_id}.objective" in invalid.json()["detail"]
    assert [step["id"] for step in (await client.get(f"/agents/{agent_id}/steps")).json()] == [
        first_id,
        step_id,
    ]

    valid = await client.patch(
        f"/agents/{agent_id}/steps/{step_id}",
        json={
            "enabled": True,
            "objective": "Collect the caller's follow-up answer.",
            "extraction_instructions": "Extract the follow-up answer.",
            "clarification_instructions": "Ask the caller to clarify.",
        },
        headers=headers,
    )
    assert valid.status_code == 200
    assert valid.json()["enabled"] is True


@pytest.mark.anyio
async def test_published_agent_rejects_reorder_that_creates_a_cycle(client, monkeypatch):
    agent_id, first_id, headers = await published_agent_with_step(client, monkeypatch)
    second = await client.post(
        f"/agents/{agent_id}/steps",
        json=question("second"),
        headers=headers,
    )
    assert second.status_code == 201
    second_id = second.json()["id"]

    branched = await client.patch(
        f"/agents/{agent_id}/steps/{first_id}",
        json={"branches": [{"conditions": [], "target_step_id": second_id}]},
        headers=headers,
    )
    assert branched.status_code == 200

    reordered = await client.put(
        f"/agents/{agent_id}/steps/order",
        json={"step_ids": [second_id, first_id]},
        headers=headers,
    )
    assert reordered.status_code == 422
    assert f"steps.{first_id}.branches" in reordered.json()["detail"]
    steps = (await client.get(f"/agents/{agent_id}/steps")).json()
    assert [(step["id"], step["sort_order"]) for step in steps] == [(first_id, 0), (second_id, 1)]
