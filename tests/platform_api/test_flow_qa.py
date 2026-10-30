"""PostgreSQL-backed tests for bounded, owner-scoped Flow QA jobs."""

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

from platform_api.app import create_app
from platform_api.auth.passwords import get_password_hasher
from platform_api.auth.sessions import create_authenticated_session
from platform_api.config import PlatformSettings
from platform_api.model_client import ModelCompletion, ModelUsage
from platform_api.models.auth import User
from platform_api.agents.models import AgentDraft, AgentVersion
from platform_api.qa.models import FlowQAJob
from tests.support.postgres import FreshPostgres


class FakeQAModelClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
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
            }
        )
        if endpoint == "generation":
            self.generation_started.set()
            if self.block_generation:
                try:
                    await self.release_generation.wait()
                except asyncio.CancelledError:
                    self.generation_cancelled.set()
                    raise
            request = json.loads(messages[-1]["content"])
            edge = request.get("expected_edge")
            assertions = []
            if edge and isinstance(edge.get("when"), dict):
                condition = edge["when"]
                if condition.get("operator") == "eq":
                    value = condition["value"]
                    field = condition["field"]
                    quote = str(value).lower() if isinstance(value, bool) else str(value)
                    assertions.append(
                        {
                            "field": field,
                            "value": value,
                            "quote": quote,
                            "intent": "answer",
                            "turn_index": 0,
                        }
                    )
            quote = assertions[0]["quote"] if assertions else "hello"
            content = json.dumps(
                {
                    "turns": [f"I explicitly state {quote}."],
                    "field_assertions": assertions,
                    "field_order": [],
                }
            )
        else:
            content = '{"reply":"I can help with that.","updates":[]}'
        return ModelCompletion(
            content=content,
            usage=ModelUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )


def _catalog_file(tmp_path: Path) -> str:
    data = {
        "llm_models": [
            {"id": "qwen3.5:4b", "name": "Runtime QA model", "supports_reasoning": True}
        ],
        "stt_providers": [
            {"id": "mlx-audio", "name": "Batch ASR", "languages": ["en", "hi"]}
        ],
        "tts_voices": [
            {"id": "af_heart", "name": "English voice", "language": "en"},
            {"id": "hf_alpha", "name": "Hindi voice", "language": "hi"},
        ],
        "generation_models": [
            {"id": "qa-generator", "name": "QA utterance generator"}
        ],
    }
    path = tmp_path / "qa-catalog.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


@pytest_asyncio.fixture
async def qa_app_client(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    tmp_path: Path,
):
    settings = test_platform_settings.model_copy(
        update={
            "catalog_path": _catalog_file(tmp_path),
            "enable_qa": True,
            "runtime_llm_base_url": "http://127.0.0.1:11434/v1",
            "generation_llm_base_url": "http://127.0.0.1:11434/v1",
        }
    )
    app = create_app(settings)
    model_client = FakeQAModelClient()
    app.state.model_client = model_client
    app.state.disable_qa_worker = True
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
        await db.flush()
        _, secret = await create_authenticated_session(
            db,
            settings,
            user=user,
            client_ip="127.0.0.1",
            user_agent="Flow QA test",
            now=now,
        )
        await db.commit()

    cookie = f"{settings.session_cookie_name}={secret}"
    boot = await client.get(
        "/api/v1/auth/session",
        headers={"Origin": settings.web_origin, "Cookie": cookie},
    )
    return user, {
        "Origin": settings.web_origin,
        "Cookie": cookie,
        "X-CSRF-Token": boot.json()["csrf_token"],
        "Content-Type": "application/json",
    }


async def _create_qa_agent(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> tuple[uuid.UUID, dict[str, object]]:
    templates = await client.get(
        "/api/v1/templates", headers={"Cookie": headers["Cookie"]}
    )
    created = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Flow QA test agent",
            "source": "template",
            "template_version_id": templates.json()["items"][0]["id"],
        },
    )
    assert created.status_code == 201
    agent_id = uuid.UUID(created.json()["id"])
    current = await client.get(
        f"/api/v1/agents/{agent_id}/draft", headers={"Cookie": headers["Cookie"]}
    )
    config = {
        **current.json()["config"],
        "tts_voice_en": "",
        "tts_voice_hi": "",
    }
    saved = await client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": current.json()["draft_revision"], "config": config},
    )
    assert saved.status_code == 200
    return agent_id, saved.json()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_flow_qa_pins_snapshot_and_keeps_suggestions_draft_only(
    qa_app_client,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = qa_app_client
    owner, owner_headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "qa_owner@example.com"
    )
    other, other_headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "qa_other@example.com"
    )
    agent_id, draft = await _create_qa_agent(client, owner_headers)

    preview_params = {
        "agent_id": str(agent_id),
        "snapshot_hash": draft["snapshot_hash"],
        "scenario_count": 1,
    }
    hidden_scenarios = await client.get(
        "/api/v1/qa/scenarios",
        params=preview_params,
        headers={"Cookie": other_headers["Cookie"]},
    )
    assert hidden_scenarios.status_code == 404

    job_request = {
        "agent_id": str(agent_id),
        "snapshot_hash": draft["snapshot_hash"],
        "scenario_count": 1,
        "max_turns": 2,
        "max_tokens": 4096,
        "max_runtime_seconds": 30,
        "utterance_model_id": "qa-generator",
    }
    queued = await client.post(
        "/api/v1/qa/jobs",
        headers=owner_headers,
        json=job_request,
    )
    assert queued.status_code == 202
    hidden_create = await client.post(
        "/api/v1/qa/jobs",
        headers=other_headers,
        json=job_request,
    )
    assert hidden_create.status_code == 404
    from platform_api.qa.worker import run_qa_job_once

    job_id = queued.json()["job_id"]
    hidden = await client.get(
        f"/api/v1/qa/jobs/{job_id}", headers={"Cookie": other_headers["Cookie"]}
    )
    assert hidden.status_code == 404
    hidden_cancel = await client.post(
        f"/api/v1/qa/jobs/{job_id}/cancel", headers=other_headers
    )
    assert hidden_cancel.status_code == 404
    await run_qa_job_once(
        app.state.session_factory,
        app.state.settings,
        app.state.catalog,
        model_client,
        uuid.UUID(job_id),
    )
    result = await client.get(
        f"/api/v1/qa/jobs/{job_id}", headers={"Cookie": owner_headers["Cookie"]}
    )
    assert result.status_code == 200
    payload = result.json()
    assert payload["status"] == "COMPLETED"
    assert payload["snapshot_hash"] == draft["snapshot_hash"]
    assert payload["scenarios"][0]["scenario_id"].startswith("qa_")
    assert payload["results"][0]["status"] == "FAIL"
    assert payload["results"][0]["turn_count"] <= payload["budget"]["max_turns"]
    assert payload["usage"]["total_tokens"] <= payload["budget"]["max_tokens"]
    assert payload["suggestions"]
    suggestion = payload["suggestions"][0]
    assert suggestion["draft_only"] is True
    assert suggestion["proposed_patch"]["append_text"]
    assert model_client.calls
    assert all(call["model"] in {"qwen3.5:4b", "qa-generator"} for call in model_client.calls)

    hidden_apply = await client.post(
        f"/api/v1/qa/jobs/{job_id}/suggestions/{suggestion['suggestion_id']}/apply",
        headers=other_headers,
        json={"expected_revision": draft["draft_revision"]},
    )
    assert hidden_apply.status_code == 404
    hidden_dismiss = await client.post(
        f"/api/v1/qa/jobs/{job_id}/suggestions/{suggestion['suggestion_id']}/dismiss",
        headers=other_headers,
    )
    assert hidden_dismiss.status_code == 404

    applied = await client.post(
        f"/api/v1/qa/jobs/{job_id}/suggestions/{suggestion['suggestion_id']}/apply",
        headers=owner_headers,
        json={"expected_revision": draft["draft_revision"]},
    )
    assert applied.status_code == 200
    assert applied.json()["draft_revision"] == draft["draft_revision"] + 1

    async with initialized_pg.sessions() as db:
        stored_draft = await db.get(AgentDraft, agent_id)
        versions = await db.execute(
            select(func.count(AgentVersion.id)).where(AgentVersion.agent_id == agent_id)
        )
    assert stored_draft is not None
    assert stored_draft.revision == draft["draft_revision"] + 1
    assert versions.scalar_one() == 0



@pytest.mark.postgres
@pytest.mark.asyncio
async def test_flow_qa_frozen_ids_quotas_and_cancellation(
    qa_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = qa_app_client
    app.state.settings = app.state.settings.model_copy(
        update={
            "qa_max_queued_jobs": 1,
            "qa_max_active_jobs_per_user": 1,
            "qa_worker_poll_seconds": 0.05,
        }
    )
    _, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "qa_limits@example.com"
    )
    agent_id, draft = await _create_qa_agent(client, headers)
    preview_url = "/api/v1/qa/scenarios"
    params = {
        "agent_id": str(agent_id),
        "snapshot_hash": draft["snapshot_hash"],
        "scenario_count": 1,
    }
    preview = await client.get(preview_url, params=params, headers=headers)
    repeated = await client.get(preview_url, params=params, headers=headers)
    assert preview.status_code == 200
    assert preview.json()["scenarios"][0]["scenario_id"] == repeated.json()["scenarios"][0]["scenario_id"]
    assert preview.json()["omitted_scenario_count"] > 0

    request = {
        "agent_id": str(agent_id),
        "snapshot_hash": draft["snapshot_hash"],
        "scenario_count": 1,
        "scenario_ids": ["qa_" + "0" * 64],
        "max_turns": 2,
        "max_tokens": 4096,
        "max_runtime_seconds": 30,
        "utterance_model_id": "qa-generator",
    }
    invalid = await client.post("/api/v1/qa/jobs", headers=headers, json=request)
    assert invalid.status_code == 422

    request["scenario_ids"] = [preview.json()["scenarios"][0]["scenario_id"]]
    first = await client.post("/api/v1/qa/jobs", headers=headers, json=request)
    assert first.status_code == 202
    second = await client.post("/api/v1/qa/jobs", headers=headers, json=request)
    assert second.status_code == 429
    first_id = first.json()["job_id"]
    cancelled = await client.post(
        f"/api/v1/qa/jobs/{first_id}/cancel", headers=headers
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"

    from platform_api.qa.worker import run_qa_job_once

    await run_qa_job_once(
        app.state.session_factory,
        app.state.settings,
        app.state.catalog,
        model_client,
        uuid.UUID(first_id),
    )
    assert model_client.calls == []

    running = await client.post("/api/v1/qa/jobs", headers=headers, json=request)
    assert running.status_code == 202
    running_id = running.json()["job_id"]
    model_client.block_generation = True
    task = asyncio.create_task(
        run_qa_job_once(
            app.state.session_factory,
            app.state.settings,
            app.state.catalog,
            model_client,
            uuid.UUID(running_id),
        )
    )
    await asyncio.wait_for(model_client.generation_started.wait(), timeout=3)
    stopped = await client.post(
        f"/api/v1/qa/jobs/{running_id}/cancel", headers=headers
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "CANCELLED"
    await asyncio.wait_for(task, timeout=3)
    assert model_client.generation_cancelled.is_set()
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_flow_qa_reclaims_expired_worker_lease_from_checkpoint(
    qa_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = qa_app_client
    _, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "qa_reclaim@example.com"
    )
    agent_id, draft = await _create_qa_agent(client, headers)
    preview = await client.get(
        "/api/v1/qa/scenarios",
        params={
            "agent_id": str(agent_id),
            "snapshot_hash": draft["snapshot_hash"],
            "scenario_count": 2,
        },
        headers=headers,
    )
    assert preview.status_code == 200
    scenario_ids = [item["scenario_id"] for item in preview.json()["scenarios"]]
    assert len(scenario_ids) == 2
    queued = await client.post(
        "/api/v1/qa/jobs",
        headers=headers,
        json={
            "agent_id": str(agent_id),
            "snapshot_hash": draft["snapshot_hash"],
            "scenario_count": 2,
            "scenario_ids": scenario_ids,
            "max_turns": 2,
            "max_tokens": 4096,
            "max_runtime_seconds": 30,
            "utterance_model_id": "qa-generator",
        },
    )
    assert queued.status_code == 202
    job_id = uuid.UUID(queued.json()["job_id"])
    async with app.state.session_factory() as db:
        job = await db.get(FlowQAJob, job_id)
        assert job is not None
        job.status = "RUNNING"
        job.lease_token = uuid.uuid4()
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        job.next_scenario_index = 1
        job.results_json = [
            {
                "scenario_id": scenario_ids[0],
                "kind": "branch",
                "status": "PASS",
                "assertion": "The recovered worker retained the saved result.",
                "expected_edge": None,
                "observed_edges": [],
                "turn_count": 0,
                "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "artifact_id": None,
                "failure_code": None,
            }
        ]
        job.updated_at = datetime.now(timezone.utc)
        await db.commit()

    from platform_api.qa.worker import run_qa_job_once

    await run_qa_job_once(
        app.state.session_factory,
        app.state.settings,
        app.state.catalog,
        model_client,
        job_id,
    )

    recovered = await client.get(
        f"/api/v1/qa/jobs/{job_id}", headers={"Cookie": headers["Cookie"]}
    )
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "COMPLETED"
    assert [item["scenario_id"] for item in recovered.json()["results"]] == scenario_ids
    generation_scenarios = [
        json.loads(call["messages"][-1]["content"])["scenario_id"]
        for call in model_client.calls
        if call["endpoint"] == "generation"
    ]
    assert generation_scenarios == [scenario_ids[1]]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_flow_qa_expired_worker_cannot_commit_before_or_after_reclaim(
    qa_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, _model_client = qa_app_client
    _, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "qa_stale_lease@example.com"
    )
    agent_id, draft = await _create_qa_agent(client, headers)
    queued = await client.post(
        "/api/v1/qa/jobs",
        headers=headers,
        json={
            "agent_id": str(agent_id),
            "snapshot_hash": draft["snapshot_hash"],
            "scenario_count": 1,
            "max_turns": 2,
            "max_tokens": 4096,
            "max_runtime_seconds": 30,
            "utterance_model_id": "qa-generator",
        },
    )
    assert queued.status_code == 202
    job_id = uuid.UUID(queued.json()["job_id"])
    from platform_api.qa.service import (
        checkpoint_qa_job,
        claim_qa_job,
        finish_qa_job,
        interrupt_qa_job,
    )

    stale_claim = await claim_qa_job(app.state.session_factory, app.state.settings, job_id=job_id)
    assert stale_claim is not None
    async with app.state.session_factory() as db:
        job = await db.get(FlowQAJob, job_id)
        assert job is not None
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

    assert not await checkpoint_qa_job(
        app.state.session_factory, stale_claim, results=[{"status": "STALE"}]
    )
    assert not await finish_qa_job(
        app.state.session_factory,
        stale_claim,
        status="COMPLETED",
        results=[{"status": "STALE"}],
    )
    assert not await interrupt_qa_job(
        app.state.session_factory, stale_claim, status="QUEUED"
    )

    current_claim = await claim_qa_job(app.state.session_factory, app.state.settings, job_id=job_id)
    assert current_claim is not None
    assert current_claim.lease_token != stale_claim.lease_token
    assert not await finish_qa_job(
        app.state.session_factory,
        stale_claim,
        status="COMPLETED",
        results=[{"status": "STALE"}],
    )
    assert await finish_qa_job(
        app.state.session_factory,
        current_claim,
        status="FAILED",
        failure_code="TEST_FINISHED",
    )
    result = await client.get(
        f"/api/v1/qa/jobs/{job_id}", headers={"Cookie": headers["Cookie"]}
    )
    assert result.status_code == 200
    assert result.json()["status"] == "FAILED"
    assert result.json()["results"] == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_flow_qa_does_not_call_model_when_prompt_exceeds_token_budget(
    qa_app_client,
    initialized_pg: FreshPostgres,
):
    client, app, model_client = qa_app_client
    _, headers = await _create_user_session(
        initialized_pg, app.state.settings, client, "qa_token_boundary@example.com"
    )
    agent_id, draft = await _create_qa_agent(client, headers)
    queued = await client.post(
        "/api/v1/qa/jobs",
        headers=headers,
        json={
            "agent_id": str(agent_id),
            "snapshot_hash": draft["snapshot_hash"],
            "scenario_count": 1,
            "max_turns": 2,
            "max_tokens": 64,
            "max_runtime_seconds": 30,
            "utterance_model_id": "qa-generator",
        },
    )
    assert queued.status_code == 202

    from platform_api.qa.worker import run_qa_job_once

    await run_qa_job_once(
        app.state.session_factory,
        app.state.settings,
        app.state.catalog,
        model_client,
        uuid.UUID(queued.json()["job_id"]),
    )

    result = await client.get(
        f"/api/v1/qa/jobs/{queued.json()['job_id']}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert result.status_code == 200
    payload = result.json()
    assert model_client.calls == []
    assert payload["status"] == "EXPIRED"
    assert payload["failure_code"] == "TOKEN_BUDGET_EXHAUSTED"
    assert payload["usage"]["total_tokens"] == 0
    assert payload["results"][0]["status"] == "INCONCLUSIVE"
    assert payload["results"][0]["failure_code"] == "TOKEN_BUDGET_EXHAUSTED"


def test_flow_qa_rejects_reachable_branch_count_above_the_bounded_catalog():
    from platform_api.qa.service import derive_scenarios

    def source(step_id: str, targets: list[str]) -> dict[str, object]:
        return {
            "id": step_id,
            "transitions": [
                {
                    "to": target,
                    "when": {
                        "operator": "eq",
                        "field": f"{step_id}_field_{index}",
                        "value": index,
                    },
                }
                for index, target in enumerate(targets)
            ],
        }

    first_targets = ["source-1", *[f"leaf-{index}" for index in range(31)]]
    second_targets = ["source-2", *[f"leaf-{index}" for index in range(31, 62)]]
    steps = [
        source("source-0", first_targets),
        source("source-1", second_targets),
        source("source-2", ["leaf-62"]),
        *[{"id": f"leaf-{index}", "transitions": []} for index in range(63)],
    ]

    with pytest.raises(ValueError, match="65 reachable branch scenarios"):
        derive_scenarios(
            {"entry_step_id": "source-0", "steps": steps},
            flow_hash="a" * 64,
            max_scenarios=64,
        )