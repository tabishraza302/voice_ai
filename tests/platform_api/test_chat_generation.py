"""PostgreSQL-backed tests for durable text chat and bounded generation jobs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from platform_api.agents.defaults import (
    DEFAULT_AGENT_TEMPLATES,
    DEFAULT_POLICY_SLUG,
    compute_policy_hash,
)
from platform_api.agents.models import Agent, AgentVersion, PlatformPromptVersion
from platform_api.auth.passwords import get_password_hasher
from platform_api.auth.sessions import create_authenticated_session
from platform_api.config import PlatformSettings
from platform_api.app import create_app
from platform_api.generation.models import GenerationJob
from platform_api.generation.service import (
    generation_worker_loop,
    run_generation_job_once,
)
from platform_api.model_client import (
    ModelClientError,
    ModelCompletion,
    ModelUsage,
    OpenAICompatibleModelClient,
)
from platform_api.models.auth import User
import platform_api.testing.chat as chat_service
from platform_api.testing.models import ChatMessage, ChatTestSession
from tests.support.postgres import FreshPostgres


class SimulatedProcessTermination(BaseException):
    """Stand in for a process exit that skips normal request cleanup."""


class FakeModelClient:
    """A per-test deterministic provider; production never uses this client."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.runtime_content = json.dumps(
            {"reply": "I can help with that.", "updates": []}
        )
        self.generation_content = "{}"
        self.crash_runtime = False
        self.fail_runtime = False
        self.fail_generation = False
        self.block_generation = False
        self.generation_started = asyncio.Event()
        self.generation_cancelled = asyncio.Event()
        self.release_generation = asyncio.Event()

    async def complete(
        self,
        endpoint: str,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        max_tokens_parameter: str,
    ) -> ModelCompletion:
        self.calls.append(
            {
                "endpoint": endpoint,
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "reasoning_effort": reasoning_effort,
                "max_tokens_parameter": max_tokens_parameter,
            }
        )
        if endpoint == "runtime":
            if self.crash_runtime:
                self.crash_runtime = False
                raise SimulatedProcessTermination
            if self.fail_runtime:
                raise RuntimeError("provider unavailable")
            content = self.runtime_content
        else:
            self.generation_started.set()
            if self.block_generation:
                try:
                    await self.release_generation.wait()
                except asyncio.CancelledError:
                    self.generation_cancelled.set()
                    raise
            if self.fail_generation:
                raise RuntimeError("provider unavailable")
            content = self.generation_content
        return ModelCompletion(
            content=content,
            usage=ModelUsage(prompt_tokens=37, completion_tokens=19, total_tokens=56),
        )


def _catalog_file(tmp_path: Path) -> str:
    data = {
        "llm_models": [
            {
                "id": "qwen3.5:4b",
                "name": "Runtime Qwen",
                "description": "runtime model",
                "supports_reasoning": True,
            }
        ],
        "stt_providers": [
            {"id": "mlx-audio", "name": "Batch ASR", "languages": ["en", "hi"]}
        ],
        "tts_voices": [
            {"id": "af_heart", "name": "English voice", "language": "en"},
            {"id": "hf_alpha", "name": "Hindi voice", "language": "hi"},
        ],
        "generation_models": [
            {
                "id": "generation-reviewed",
                "name": "Reviewed generator",
                "description": "Operator-qualified generation model",
                "supports_reasoning": False,
            }
        ],
    }
    path = tmp_path / "task4-catalog.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


@pytest_asyncio.fixture
async def task4_app_client(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    tmp_path: Path,
):
    """Run the platform app with a fake provider and a real isolated PostgreSQL schema."""
    settings = test_platform_settings.model_copy(
        update={
            "catalog_path": _catalog_file(tmp_path),
            "enable_generation": True,
            "enable_chat_test": True,
            "runtime_llm_base_url": "http://runtime.invalid/v1",
            "generation_llm_base_url": "http://generation.invalid/v1",
        }
    )
    app = create_app(settings)
    model_client = FakeModelClient()
    app.state.model_client = model_client
    app.state.disable_generation_worker = True
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            yield client, app, model_client


async def _create_user_session(
    initialized_pg: FreshPostgres,
    settings: PlatformSettings,
    client: httpx.AsyncClient,
    email: str,
) -> tuple[User, dict[str, str]]:
    now = datetime.now(timezone.utc)
    password_hash = await get_password_hasher().hash_password("secure-password-12345")
    async with initialized_pg.sessions() as db:
        user = User(
            id=uuid.uuid4(),
            email=email,
            email_key=email.lower(),
            display_name=email.split("@", maxsplit=1)[0],
            password_hash=password_hash,
            role="user",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        db.add(user)
        _, secret = await create_authenticated_session(
            db,
            settings,
            user=user,
            client_ip="127.0.0.1",
            user_agent="Task4 test",
            now=now,
        )
        await db.commit()

    cookie = f"{settings.session_cookie_name}={secret}"
    boot = await client.get(
        "/api/v1/auth/session",
        headers={"Origin": settings.web_origin, "Cookie": cookie},
    )
    token = boot.json()["csrf_token"]
    return user, {
        "Origin": settings.web_origin,
        "Cookie": cookie,
        "X-CSRF-Token": token,
        "Content-Type": "application/json",
    }


async def _create_template_agent(
    client: httpx.AsyncClient,
    headers: dict[str, str],
) -> tuple[str, dict[str, object]]:
    templates = await client.get(
        "/api/v1/templates", headers={"Cookie": headers["Cookie"]}
    )
    assert templates.status_code == 200
    created = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Chat test agent",
            "source": "template",
            "template_version_id": templates.json()["items"][0]["id"],
        },
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]
    draft = await client.get(
        f"/api/v1/agents/{agent_id}/draft",
        headers={"Cookie": headers["Cookie"]},
    )
    assert draft.status_code == 200
    draft_body = draft.json()
    text_only_config = {
        **draft_body["config"],
        "tts_voice_en": "",
        "tts_voice_hi": "",
    }
    text_only = await client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={
            "expected_revision": draft_body["draft_revision"],
            "config": text_only_config,
        },
    )
    assert text_only.status_code == 200
    text_only_body = text_only.json()
    assert isinstance(text_only_body["snapshot_hash"], str)
    assert len(text_only_body["snapshot_hash"]) == 64
    return agent_id, text_only_body


def _generated_proposal() -> dict[str, object]:
    template = DEFAULT_AGENT_TEMPLATES[0]
    return {
        "name": "Generated loan guide",
        "description": "A bilingual loan qualification draft.",
        "persona_prompt": "Be precise and do not invent product terms.",
        "config": {
            "flow": template["flow"],
            "llm_model": "qwen3.5:4b",
            "stt_provider": "mlx-audio",
            "tts_voice_en": "af_heart",
            "tts_voice_hi": "hf_alpha",
        },
        "unknown_business_facts": [
            "The lender has not provided interest rates."
        ],
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_generation_uses_selected_model_budget_and_never_saves_agent(
    task4_app_client,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = task4_app_client
    owner, owner_headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "generation_owner@example.com"
    )
    other, other_headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "generation_other@example.com"
    )

    listed = await client.get(
        "/api/v1/generation-models", headers={"Cookie": owner_headers["Cookie"]}
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == ["generation-reviewed"]

    model_client.generation_content = json.dumps(_generated_proposal())
    queued = await client.post(
        "/api/v1/agents/generate",
        headers=owner_headers,
        json={
            "requirement": "Create a bilingual loan intake flow.",
            "generation_model_id": "generation-reviewed",
            "language": "hi",
            "token_budget": 768,
        },
    )
    assert queued.status_code == 202
    job_id = queued.json()["job_id"]
    assert queued.json()["status"] == "QUEUED"
    assert queued.headers["retry-after"] == "2"

    hidden = await client.get(
        f"/api/v1/generation-jobs/{job_id}",
        headers={"Cookie": other_headers["Cookie"]},
    )
    assert hidden.status_code == 404

    await run_generation_job_once(
        app.state.session_factory,
        app.state.settings,
        app.state.catalog,
        model_client,
        uuid.UUID(job_id),
    )
    completed = await client.get(
        f"/api/v1/generation-jobs/{job_id}",
        headers={"Cookie": owner_headers["Cookie"]},
    )
    body = completed.json()
    assert completed.status_code == 200
    assert body["status"] == "COMPLETED"
    assert body["proposal"]["config"]["flow"] == DEFAULT_AGENT_TEMPLATES[0]["flow"]
    assert body["unknown_business_facts"] == [
        "The lender has not provided interest rates."
    ]
    assert body["usage"] == {
        "prompt_tokens": 37,
        "completion_tokens": 19,
        "total_tokens": 56,
    }
    call = model_client.calls[-1]
    assert call["endpoint"] == "generation"
    assert call["model"] == "generation-reviewed"
    assert call["max_tokens"] == 768
    assert "hi" in str(call["messages"])

    async with initialized_pg.sessions() as db:
        owner_agents = await db.execute(
            select(func.count(Agent.id)).where(Agent.owner_user_id == owner.id)
        )
        assert owner_agents.scalar_one() == 0
        saved_job = await db.get(GenerationJob, uuid.UUID(job_id))
        assert saved_job is not None
        assert saved_job.proposal_json is not None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_generation_queue_limits_cancel_and_model_failure_do_not_fabricate(
    task4_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = task4_app_client
    owner, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "generation_limits@example.com"
    )
    app.state.settings.generation_max_queued_jobs = 1

    request = {
        "requirement": "Create an intake flow.",
        "generation_model_id": "generation-reviewed",
        "token_budget": 128,
    }
    queued = await client.post("/api/v1/agents/generate", headers=headers, json=request)
    assert queued.status_code == 202
    job_id = queued.json()["job_id"]

    full = await client.post("/api/v1/agents/generate", headers=headers, json=request)
    assert full.status_code == 429
    assert int(full.headers["retry-after"]) >= 1

    cancelled = await client.post(
        f"/api/v1/generation-jobs/{job_id}/cancel", headers=headers, json={}
    )
    assert cancelled.status_code == 200
    assert cancelled.json() == {"job_id": job_id, "status": "CANCELLED"}
    await run_generation_job_once(
        app.state.session_factory,
        app.state.settings,
        app.state.catalog,
        model_client,
        uuid.UUID(job_id),
    )
    assert model_client.calls == []

    model_client.generation_content = "not JSON"
    invalid = await client.post("/api/v1/agents/generate", headers=headers, json=request)
    assert invalid.status_code == 202
    invalid_id = invalid.json()["job_id"]
    await run_generation_job_once(
        app.state.session_factory,
        app.state.settings,
        app.state.catalog,
        model_client,
        uuid.UUID(invalid_id),
    )
    failed = await client.get(
        f"/api/v1/generation-jobs/{invalid_id}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert failed.json()["status"] == "FAILED"
    assert failed.json()["proposal"] is None
    assert failed.json()["usage"] == {
        "prompt_tokens": 37,
        "completion_tokens": 19,
        "total_tokens": 56,
    }

    foreign_cancel = await client.post(
        f"/api/v1/generation-jobs/{invalid_id}/cancel",
        headers={**headers, "Cookie": ""},
        json={},
    )
    assert foreign_cancel.status_code in {401, 403}
    async with initialized_pg.sessions() as db:
        saved = await db.get(GenerationJob, uuid.UUID(invalid_id))
        assert saved is not None
        assert saved.status == "FAILED"
        assert saved.proposal_json is None
        assert saved.usage_json == {
            "prompt_tokens": 37,
            "completion_tokens": 19,
            "total_tokens": 56,
        }
        assert owner.id == saved.owner_user_id



@pytest.mark.postgres
@pytest.mark.asyncio
async def test_generation_worker_survives_running_cancel_and_keeps_daily_reservation(
    task4_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = task4_app_client
    first_owner, first_headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "generation_cancelled@example.com"
    )
    second_owner, second_headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "generation_after_cancel@example.com"
    )
    app.state.settings.generation_daily_token_budget = 128
    app.state.settings.generation_worker_poll_seconds = 0.05
    model_client.block_generation = True
    worker = asyncio.create_task(generation_worker_loop(app))
    request = {
        "requirement": "Create an intake flow.",
        "generation_model_id": "generation-reviewed",
        "token_budget": 128,
    }

    try:
        first = await client.post(
            "/api/v1/agents/generate",
            headers=first_headers,
            json=request,
        )
        assert first.status_code == 202
        first_job_id = first.json()["job_id"]
        await asyncio.wait_for(model_client.generation_started.wait(), timeout=5)

        cancel_app = create_app(app.state.settings)
        cancel_app.state.disable_generation_worker = True
        async with cancel_app.router.lifespan_context(cancel_app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=cancel_app),
                base_url="http://127.0.0.1:8000",
            ) as cancel_client:
                cancelled = await cancel_client.post(
                    f"/api/v1/generation-jobs/{first_job_id}/cancel",
                    headers=first_headers,
                    json={},
                )
                assert cancelled.status_code == 200
                assert cancelled.json()["status"] == "CANCELLING"
        await asyncio.wait_for(model_client.generation_cancelled.wait(), timeout=5)

        denied = await client.post(
            "/api/v1/agents/generate",
            headers=first_headers,
            json=request,
        )
        assert denied.status_code == 429

        model_client.block_generation = False
        model_client.generation_content = json.dumps(_generated_proposal())
        second = await client.post(
            "/api/v1/agents/generate",
            headers=second_headers,
            json=request,
        )
        assert second.status_code == 202
        second_job_id = second.json()["job_id"]

        completed = None
        for _ in range(100):
            completed = await client.get(
                f"/api/v1/generation-jobs/{second_job_id}",
                headers={"Cookie": second_headers["Cookie"]},
            )
            if completed.json()["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert completed is not None
        assert completed.json()["status"] == "COMPLETED"
        cancelled_status = None
        for _ in range(100):
            cancelled_status = await client.get(
                f"/api/v1/generation-jobs/{first_job_id}",
                headers={"Cookie": first_headers["Cookie"]},
            )
            if cancelled_status.json()["status"] == "CANCELLED":
                break
            await asyncio.sleep(0.05)
        assert cancelled_status is not None
        assert cancelled_status.json()["status"] == "CANCELLED"
        assert cancelled_status.json()["usage"] == {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        assert first_owner.id != second_owner.id
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert worker.cancelled()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_shutdown_requeues_generation_with_unknown_usage(
    task4_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = task4_app_client
    _, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "generation_restart@example.com"
    )
    model_client.generation_content = json.dumps(_generated_proposal())
    model_client.block_generation = True
    queued = await client.post(
        "/api/v1/agents/generate",
        headers=headers,
        json={
            "requirement": "Create an intake flow.",
            "generation_model_id": "generation-reviewed",
            "token_budget": 64,
        },
    )
    assert queued.status_code == 202
    job_id = uuid.UUID(queued.json()["job_id"])

    interrupted = asyncio.create_task(
        run_generation_job_once(
            app.state.session_factory,
            app.state.settings,
            app.state.catalog,
            app.state.model_client,
            job_id,
        )
    )
    await asyncio.wait_for(model_client.generation_started.wait(), timeout=5)
    interrupted.cancel()
    result = await asyncio.gather(interrupted, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)

    unknown_usage = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    async with app.state.session_factory() as db:
        job = await db.get(GenerationJob, job_id)
        assert job is not None
        assert job.status == "QUEUED"
        assert job.attempt_count == 1
        assert job.usage_json == unknown_usage
        assert job.lease_token is None
        assert job.lease_expires_at is None

    model_client.release_generation.set()
    await run_generation_job_once(
        app.state.session_factory,
        app.state.settings,
        app.state.catalog,
        app.state.model_client,
        job_id,
    )
    async with app.state.session_factory() as db:
        job = await db.get(GenerationJob, job_id)
        assert job is not None
        assert job.status == "COMPLETED"
        assert job.attempt_count == 2
        assert job.usage_json == unknown_usage
        assert job.proposal_json is not None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pending_chat_turn_resumes_after_runtime_process_restart(
    task4_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = task4_app_client
    owner, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "chat_restart@example.com"
    )
    agent_id, draft = await _create_template_agent(client, headers)

    missing_id = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json={
            "snapshot_hash": draft["snapshot_hash"],
            "expected_turn_sequence": 1,
            "text": "I am salaried.",
        },
    )
    assert missing_id.status_code == 422
    test_id = str(uuid.uuid4())
    turn = {
        "snapshot_hash": draft["snapshot_hash"],
        "test_id": test_id,
        "expected_turn_sequence": 1,
        "text": "I am salaried.",
    }
    model_client.crash_runtime = True
    with pytest.raises(SimulatedProcessTermination):
        await client.post(
            f"/api/v1/agents/{agent_id}/test/chat",
            headers=headers,
            json=turn,
        )

    recovered = await client.get(
        f"/api/v1/agents/{agent_id}/test/chat/{test_id}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert recovered.status_code == 200
    assert recovered.json()["pending_turn_sequence"] == 1
    assert [message["role"] for message in recovered.json()["messages"]] == ["user"]

    still_running = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json=turn,
    )
    assert still_running.status_code == 409

    async with app.state.session_factory() as db:
        chat_session = await db.get(ChatTestSession, uuid.UUID(test_id))
        assert chat_session is not None
        assert chat_session.owner_user_id == owner.id
        assert chat_session.pending_turn_id is not None
        assert chat_session.pending_generation_id is not None
        chat_session.pending_lease_expires_at = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )
        await db.commit()

    resumed = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json=turn,
    )
    assert resumed.status_code == 200
    assert resumed.json()["turn_sequence"] == 1
    assert resumed.json()["reply"] == "I can help with that."
    recovered = await client.get(
        f"/api/v1/agents/{agent_id}/test/chat/{test_id}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert recovered.status_code == 200
    assert recovered.json()["pending_turn_sequence"] is None
    assert [message["role"] for message in recovered.json()["messages"]] == [
        "user",
        "assistant",
    ]
    assert len([call for call in model_client.calls if call["endpoint"] == "runtime"]) == 2


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_terminal_pending_chat_turn_resumes_without_model(
    task4_app_client,
    initialized_pg: FreshPostgres,
    monkeypatch,
):
    client, app, model_client = task4_app_client
    _, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "chat_terminal_restart@example.com"
    )
    agent_id, draft = await _create_template_agent(client, headers)
    test_id = str(uuid.uuid4())
    turn = {
        "snapshot_hash": draft["snapshot_hash"],
        "test_id": test_id,
        "expected_turn_sequence": 1,
        "text": "stop",
    }
    finish_without_generation = chat_service._finish_without_generation
    crash_before_finalize = True

    async def interrupt_after_commit(db, **kwargs):
        nonlocal crash_before_finalize
        if crash_before_finalize:
            crash_before_finalize = False
            raise SimulatedProcessTermination
        return await finish_without_generation(db, **kwargs)

    monkeypatch.setattr(
        chat_service, "_finish_without_generation", interrupt_after_commit
    )
    with pytest.raises(SimulatedProcessTermination):
        await client.post(
            f"/api/v1/agents/{agent_id}/test/chat",
            headers=headers,
            json=turn,
        )

    recovered = await client.get(
        f"/api/v1/agents/{agent_id}/test/chat/{test_id}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert recovered.status_code == 200
    assert recovered.json()["outcome"] == "aborted"
    assert recovered.json()["pending_turn_sequence"] == 1
    assert [message["role"] for message in recovered.json()["messages"]] == ["user"]

    async with app.state.session_factory() as db:
        chat_session = await db.get(ChatTestSession, uuid.UUID(test_id))
        assert chat_session is not None
        chat_session.pending_lease_expires_at = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )
        await db.commit()

    resumed = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json=turn,
    )
    assert resumed.status_code == 200
    assert resumed.json()["outcome"] == "aborted"
    assert resumed.json()["reply"] == ""
    assert resumed.json()["turn_sequence"] == 1
    assert model_client.calls == []
    recovered = await client.get(
        f"/api/v1/agents/{agent_id}/test/chat/{test_id}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert recovered.status_code == 200
    assert recovered.json()["pending_turn_sequence"] is None
    assert [message["role"] for message in recovered.json()["messages"]] == ["user"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_chat_ignores_voice_qualification_required_for_publish(
    task4_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = task4_app_client
    _, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "chat_voice_mode@example.com"
    )
    agent_id, draft = await _create_template_agent(client, headers)
    configured = await client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={
            "expected_revision": draft["draft_revision"],
            "config": {
                **draft["config"],
                "tts_voice_en": "af_heart",
                "tts_voice_hi": "hf_alpha",
            },
        },
    )
    assert configured.status_code == 200
    draft = configured.json()
    assert any(
        error["code"] == "PLAYBACK_UNQUALIFIED"
        for error in draft["validation"]["errors"]
    )
    assert isinstance(draft["snapshot_hash"], str)
    assert len(draft["snapshot_hash"]) == 64

    blocked_publish = await client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**headers, "Idempotency-Key": "publish-voice-test"},
        json={
            "expected_revision": draft["draft_revision"],
            "acknowledged_warning_codes": [],
        },
    )
    assert blocked_publish.status_code == 422
    assert any(
        field["code"] == "PLAYBACK_UNQUALIFIED"
        for field in blocked_publish.json()["error"]["fields"]
    )

    test_id = str(uuid.uuid4())
    chat = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json={
            "snapshot_hash": draft["snapshot_hash"],
            "test_id": test_id,
            "expected_turn_sequence": 1,
            "text": "I am salaried.",
        },
    )
    assert chat.status_code == 200
    assert chat.json()["test_id"] == test_id
    assert chat.json()["reply"] == "I can help with that."
    assert len(model_client.calls) == 1
    assert model_client.calls[0]["endpoint"] == "runtime"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_chat_uses_immutable_published_snapshot_after_draft_changes(
    task4_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = task4_app_client
    owner, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "chat_published@example.com"
    )
    agent_id, draft = await _create_template_agent(client, headers)
    agent_view = await client.get(
        f"/api/v1/agents/{agent_id}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert agent_view.status_code == 200
    original_persona = agent_view.json()["persona_prompt"]
    original_welcome = draft["config"]["welcome"]["en"]
    warning_codes = [
        warning["code"] for warning in draft["validation"]["warnings"]
    ]
    published = await client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**headers, "Idempotency-Key": "publish-chat-snapshot"},
        json={
            "expected_revision": draft["draft_revision"],
            "acknowledged_warning_codes": warning_codes,
        },
    )
    assert published.status_code == 201
    artifact_hash = published.json()["artifact_hash"]
    version_id = published.json()["version_id"]

    changed_welcome = "This welcome exists only in the later draft."
    changed_config = draft["config"]
    changed_config["welcome"]["en"] = changed_welcome
    updated_draft = await client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={
            "expected_revision": draft["draft_revision"],
            "config": changed_config,
        },
    )
    assert updated_draft.status_code == 200
    changed_persona = "This persona exists only in the later draft."
    updated_agent = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=headers,
        json={"persona_prompt": changed_persona},
    )
    assert updated_agent.status_code == 200

    test_id = str(uuid.uuid4())
    response = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json={
            "snapshot_hash": artifact_hash,
            "test_id": test_id,
            "expected_turn_sequence": 1,
            "text": "I am salaried.",
        },
    )
    assert response.status_code == 200
    assert response.json()["test_id"] == test_id
    assert response.json()["turn_sequence"] == 1
    assert response.json()["reply"] == "I can help with that."

    model_context = str(model_client.calls[-1]["messages"])
    assert original_persona in model_context
    assert changed_persona not in model_context
    assert original_welcome in model_context
    assert changed_welcome not in model_context
    recovered = await client.get(
        f"/api/v1/agents/{agent_id}/test/chat/{test_id}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert recovered.status_code == 200
    assert recovered.json()["snapshot_hash"] == artifact_hash
    assert [message["role"] for message in recovered.json()["messages"]] == [
        "user",
        "assistant",
    ]

    async with initialized_pg.sessions() as db:
        chat_session = await db.get(ChatTestSession, uuid.UUID(test_id))
        version = await db.get(AgentVersion, uuid.UUID(version_id))
        assert chat_session is not None
        assert version is not None
        assert chat_session.owner_user_id == owner.id
        assert chat_session.snapshot_kind == "published"
        assert chat_session.snapshot_json["version_id"] == version_id
        assert (
            chat_session.snapshot_json["persona_prompt"]
            == version.immutable_config_json["persona_prompt"]
            == original_persona
        )

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_chat_pins_snapshot_rejects_stale_turn_and_recovers_durable_messages(
    task4_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = task4_app_client
    owner, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "chat_owner@example.com"
    )
    agent_id, draft = await _create_template_agent(client, headers)
    first_snapshot = draft["snapshot_hash"]
    assert isinstance(first_snapshot, str) and len(first_snapshot) == 64
    test_id = str(uuid.uuid4())

    first = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json={
            "snapshot_hash": first_snapshot,
            "test_id": test_id,
            "expected_turn_sequence": 1,
            "text": "I am salaried.",
        },
    )
    assert first.status_code == 200
    assert first.json()["test_id"] == test_id
    assert first.json()["turn_sequence"] == 1
    assert first.json()["reply"] == "I can help with that."
    assert first.json()["flow_state"]["version"] >= 1

    edited_config = draft["config"]
    edited_config["welcome"]["en"] = "A changed welcome."
    updated = await client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": draft["draft_revision"], "config": edited_config},
    )
    assert updated.status_code == 200
    assert updated.json()["snapshot_hash"] != first_snapshot

    stale = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json={
            "snapshot_hash": first_snapshot,
            "test_id": test_id,
            "expected_turn_sequence": 1,
            "text": "Retry the same first turn.",
        },
    )
    assert stale.status_code == 409
    assert len(model_client.calls) == 1

    next_turn = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json={
            "snapshot_hash": first_snapshot,
            "test_id": test_id,
            "expected_turn_sequence": 2,
            "text": "My monthly income is 50000 INR.",
        },
    )
    assert next_turn.status_code == 200
    assert next_turn.json()["turn_sequence"] == 2
    model_context = str(model_client.calls[-1]["messages"])
    assert "A changed welcome." not in model_context

    persona_changed = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=headers,
        json={"persona_prompt": "A changed persona."},
    )
    assert persona_changed.status_code == 200
    current_draft = await client.get(
        f"/api/v1/agents/{agent_id}/draft",
        headers={"Cookie": headers["Cookie"]},
    )
    assert current_draft.status_code == 200
    assert current_draft.json()["snapshot_hash"] != updated.json()["snapshot_hash"]

    stale_start = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json={
            "snapshot_hash": updated.json()["snapshot_hash"],
            "test_id": str(uuid.uuid4()),
            "expected_turn_sequence": 1,
            "text": "Start with an outdated persona.",
        },
    )
    assert stale_start.status_code == 409
    assert len(model_client.calls) == 2

    recovered = await client.get(
        f"/api/v1/agents/{agent_id}/test/chat/{test_id}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert recovered.status_code == 200
    assert recovered.json()["snapshot_hash"] == first_snapshot
    assert [message["role"] for message in recovered.json()["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert recovered.json()["turn_sequence"] == 2
    assert recovered.json()["trace"]

    async with initialized_pg.sessions() as db:
        chat_session = await db.get(ChatTestSession, uuid.UUID(test_id))
        assert chat_session is not None
        messages = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.test_id == chat_session.id)
            .order_by(ChatMessage.turn_sequence, ChatMessage.role)
        )
        assert len(messages.scalars().all()) == 4

    reset_id = str(uuid.uuid4())
    reset = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json={
            "snapshot_hash": current_draft.json()["snapshot_hash"],
            "test_id": reset_id,
            "expected_turn_sequence": 1,
            "text": "Start over.",
        },
    )
    assert reset.status_code == 200
    assert reset.json()["test_id"] == reset_id
    assert reset.json()["turn_sequence"] == 1

    other, other_headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "chat_other@example.com"
    )
    isolated = await client.get(
        f"/api/v1/agents/{agent_id}/test/chat/{test_id}",
        headers={"Cookie": other_headers["Cookie"]},
    )
    assert isolated.status_code == 404
    assert other.id != owner.id

    assert "A changed persona." not in model_context


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_chat_rejects_new_tests_with_stale_policy_but_pins_existing_sessions(
    task4_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = task4_app_client
    _, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "chat_policy@example.com"
    )
    agent_id, draft = await _create_template_agent(client, headers)
    test_id = str(uuid.uuid4())

    first = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json={
            "snapshot_hash": draft["snapshot_hash"],
            "test_id": test_id,
            "expected_turn_sequence": 1,
            "text": "I am salaried.",
        },
    )
    assert first.status_code == 200

    policy_text = "A newly activated test policy."
    async with initialized_pg.sessions() as db:
        db.add(
            PlatformPromptVersion(
                id=uuid.uuid4(),
                slug=DEFAULT_POLICY_SLUG,
                version=1,
                prompt_text=policy_text,
                policy_hash=compute_policy_hash(policy_text),
                is_active=True,
                created_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    stale_start = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json={
            "snapshot_hash": draft["snapshot_hash"],
            "test_id": str(uuid.uuid4()),
            "expected_turn_sequence": 1,
            "text": "Start using the old policy.",
        },
    )
    assert stale_start.status_code == 409
    assert len(model_client.calls) == 1

    next_turn = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json={
            "snapshot_hash": draft["snapshot_hash"],
            "test_id": test_id,
            "expected_turn_sequence": 2,
            "text": "My monthly income is 50000 INR.",
        },
    )
    assert next_turn.status_code == 200
    assert len(model_client.calls) == 2
    assert policy_text not in str(model_client.calls[-1]["messages"])


@pytest.mark.asyncio
async def test_model_client_omits_unsupported_structured_output_option(monkeypatch):
    settings = PlatformSettings(
        runtime_llm_base_url="http://127.0.0.1:11434/v1"
    )
    model_client = OpenAICompatibleModelClient(settings)
    requests: list[dict[str, object]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(await request.aread()))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"reply":"Hello.","updates":[]}'
                        }
                    }
                ]
            },
        )

    transport = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(
        model_client,
        "_client",
        lambda _endpoint, _timeout: transport,
    )
    try:
        completion = await model_client.complete(
            "runtime",
            model="qwen3.5:4b",
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": "Say hello."},
            ],
            max_tokens=64,
            temperature=0.2,
            reasoning_effort="none",
            max_tokens_parameter="max_tokens",
        )
    finally:
        await transport.aclose()

    assert completion.content == '{"reply":"Hello.","updates":[]}'
    assert len(requests) == 1
    assert "response_format" not in requests[0]


@pytest.mark.asyncio
async def test_model_client_exposes_safe_upstream_http_status(monkeypatch):
    settings = PlatformSettings(
        runtime_llm_base_url="http://127.0.0.1:11434/v1"
    )
    model_client = OpenAICompatibleModelClient(settings)

    async def reject(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            501,
            json={"error": {"message": "private provider details"}},
        )

    transport = httpx.AsyncClient(transport=httpx.MockTransport(reject))
    monkeypatch.setattr(
        model_client,
        "_client",
        lambda _endpoint, _timeout: transport,
    )
    try:
        with pytest.raises(ModelClientError) as error:
            await model_client.complete(
                "runtime",
                model="qwen3.5:4b",
                messages=[{"role": "user", "content": "Say hello."}],
                max_tokens=64,
                temperature=0.2,
                reasoning_effort=None,
                max_tokens_parameter="max_tokens",
            )
    finally:
        await transport.aclose()

    assert error.value.code == "MODEL_HTTP_501"
    assert error.value.public_message == (
        "The configured model endpoint returned HTTP 501."
    )
    assert "private provider details" not in error.value.public_message

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_chat_model_failure_commits_only_user_turn_without_fallback(
    task4_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = task4_app_client
    _, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "chat_failure@example.com"
    )
    agent_id, draft = await _create_template_agent(client, headers)
    model_client.fail_runtime = True
    test_id = str(uuid.uuid4())

    response = await client.post(
        f"/api/v1/agents/{agent_id}/test/chat",
        headers=headers,
        json={
            "snapshot_hash": draft["snapshot_hash"],
            "test_id": test_id,
            "expected_turn_sequence": 1,
            "text": "I have a question.",
        },
    )
    assert response.status_code == 200
    assert response.json()["reply"] == ""
    assert response.json()["outcome"] == "failed"
    recovered = await client.get(
        f"/api/v1/agents/{agent_id}/test/chat/{test_id}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert recovered.status_code == 200
    assert [message["role"] for message in recovered.json()["messages"]] == ["user"]
    assert recovered.json()["flow_state"]["outcome"] == "failed"
    failure_trace = next(
        entry
        for entry in recovered.json()["trace"]
        if entry["event_type"] == "generation_failed"
    )
    assert failure_trace["diagnostics"] == [
        {
            "code": "MODEL_REQUEST_FAILED",
            "path": "runtime_model",
            "severity": "error",
            "message": "The configured model request failed unexpectedly.",
        }
    ]
