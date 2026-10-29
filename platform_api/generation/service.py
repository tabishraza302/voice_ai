"""PostgreSQL-backed bounded generation queue and proposal validation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any
import uuid

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.flow.schemas import FlowLimits
from platform_api.agents.config import AgentConfigError, extract_settings
from platform_api.agents.security import find_secret_paths
from platform_api.agents.schemas import DiagnosticView
from platform_api.agents.validation import (
    secret_diagnostics,
    validate_catalog_eligibility,
    validate_draft_semantics,
    validate_draft_structure,
)
from platform_api.auth.errors import (
    NotFoundError,
    RateLimitedError,
    ValidationError,
)
from platform_api.catalog import ModelCatalog
from platform_api.config import PlatformSettings
from platform_api.generation.models import GenerationJob, MAX_GENERATION_ATTEMPTS
from platform_api.generation.schemas import AgentProposal, GenerationModelOutput
from platform_api.model_client import ModelClientError, ModelCompletion, ModelUsage
from platform_api.models.auth import User


logger = logging.getLogger(__name__)
GENERATION_QUEUE_ADVISORY_LOCK = 7_421_925_101
JOB_STATUS_VALUES = (
    "QUEUED",
    "RUNNING",
    "CANCELLING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
)


@dataclass(frozen=True, slots=True)
class ClaimedGenerationJob:
    id: uuid.UUID
    owner_user_id: uuid.UUID
    requirement: str
    generation_model_id: str
    language: str
    token_budget: int
    lease_token: uuid.UUID


@dataclass(frozen=True, slots=True)
class ValidatedProposal:
    proposal: AgentProposal
    unknown_business_facts: list[str]
    validation_warnings: list[dict[str, str]]


async def enqueue_generation_job(
    db: AsyncSession,
    *,
    settings: PlatformSettings,
    catalog: ModelCatalog,
    owner_user_id: uuid.UUID,
    requirement: str,
    generation_model_id: str,
    language: str,
    token_budget: int,
) -> GenerationJob:
    """Reserve bounded queue and owner quotas before creating a durable job."""
    if token_budget > settings.generation_max_token_budget:
        raise ValidationError(
            "token_budget exceeds the configured maximum",
            fields=[{"field": "token_budget", "code": "TOKEN_BUDGET_TOO_LARGE"}],
        )
    if token_budget > settings.generation_daily_token_budget:
        raise ValidationError(
            "token_budget exceeds the per-owner daily generation budget",
            fields=[{"field": "token_budget", "code": "TOKEN_BUDGET_TOO_LARGE"}],
        )
    if generation_model_id not in {item.id for item in catalog.generation_models}:
        raise ValidationError(
            "generation_model_id is not an operator-qualified generation model",
            fields=[{"field": "generation_model_id", "code": "MODEL_NOT_ALLOWED"}],
        )

    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": GENERATION_QUEUE_ADVISORY_LOCK},
    )
    await db.execute(select(User.id).where(User.id == owner_user_id).with_for_update())

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    user_row = await db.execute(select(User.id).where(User.id == owner_user_id))
    if user_row.scalar_one_or_none() is None:
        raise NotFoundError()

    active_lease = and_(
        GenerationJob.lease_token.is_not(None),
        GenerationJob.lease_expires_at > now,
    )
    owner_active = await db.execute(
        select(func.count(GenerationJob.id)).where(
            GenerationJob.owner_user_id == owner_user_id,
            or_(GenerationJob.status == "QUEUED", active_lease),
        )
    )
    if owner_active.scalar_one() >= settings.generation_max_active_jobs_per_user:
        raise RateLimitedError(
            "The owner already has the maximum number of active generation jobs",
            retry_after=settings.generation_poll_after_seconds,
        )

    queued_count = await db.execute(
        select(func.count(GenerationJob.id)).where(GenerationJob.status == "QUEUED")
    )
    if queued_count.scalar_one() >= settings.generation_max_queued_jobs:
        raise RateLimitedError(
            "The generation queue is full",
            retry_after=settings.generation_poll_after_seconds,
        )

    reserved_filter = and_(
        GenerationJob.owner_user_id == owner_user_id,
        GenerationJob.created_at >= day_ago,
        or_(
            GenerationJob.status != "CANCELLED",
            GenerationJob.started_at.is_not(None),
        ),
    )
    reserved = await db.execute(
        select(
            func.coalesce(
                func.sum(
                    GenerationJob.token_budget
                    * func.greatest(GenerationJob.attempt_count, 1)
                ),
                0,
            )
        ).where(reserved_filter)
    )
    reserved_tokens = int(reserved.scalar_one())
    if reserved_tokens + token_budget > settings.generation_daily_token_budget:
        oldest_result = await db.execute(
            select(func.min(GenerationJob.created_at)).where(reserved_filter)
        )
        oldest = oldest_result.scalar_one_or_none()
        retry_after = settings.generation_poll_after_seconds
        if oldest is not None:
            retry_after = max(
                1,
                int((oldest + timedelta(hours=24) - now).total_seconds()),
            )
        raise RateLimitedError(
            "The per-owner 24-hour generation token budget is exhausted",
            retry_after=retry_after,
        )

    job = GenerationJob(
        id=uuid.uuid4(),
        owner_user_id=owner_user_id,
        requirement=requirement,
        generation_model_id=generation_model_id,
        language=language,
        token_budget=token_budget,
        status="QUEUED",
        unknown_business_facts_json=[],
        validation_warnings_json=[],
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    await db.flush()
    return job


async def cancel_generation_job(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    job_id: uuid.UUID,
) -> GenerationJob:
    """Request cancellation for a queued or running job without exposing
    another owner's record."""
    result = await db.execute(
        select(GenerationJob)
        .where(
            GenerationJob.id == job_id,
            GenerationJob.owner_user_id == owner_user_id,
        )
        .with_for_update()
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise NotFoundError()
    if job.status == "QUEUED":
        job.status = "CANCELLED"
        job.completed_at = datetime.now(timezone.utc)
        job.updated_at = job.completed_at
        job.failure_code = None
        job.proposal_json = None
        job.unknown_business_facts_json = []
    elif job.status == "RUNNING":
        now = datetime.now(timezone.utc)
        job.status = "CANCELLING"
        job.cancel_requested_at = now
        job.updated_at = now
        job.failure_code = None
        job.proposal_json = None
        job.unknown_business_facts_json = []
    return job


async def claim_generation_job(
    session_factory: async_sessionmaker[AsyncSession],
    settings: PlatformSettings,
    *,
    job_id: uuid.UUID | None = None,
) -> ClaimedGenerationJob | None:
    """Claim one queued row under a short PostgreSQL transaction and lease."""
    async with session_factory() as db:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": GENERATION_QUEUE_ADVISORY_LOCK},
        )
        now = datetime.now(timezone.utc)
        await _expire_stale_jobs(db, settings, now)

        active_lease = and_(
            GenerationJob.lease_token.is_not(None),
            GenerationJob.lease_expires_at > now,
        )
        active_count_result = await db.execute(
            select(func.count(GenerationJob.id)).where(active_lease)
        )
        if active_count_result.scalar_one() >= settings.generation_max_running_jobs:
            await db.commit()
            return None

        blocked_owners = (
            select(GenerationJob.owner_user_id)
            .where(active_lease)
            .group_by(GenerationJob.owner_user_id)
            .having(func.count(GenerationJob.id) >= settings.generation_max_active_jobs_per_user)
        )
        statement = select(GenerationJob).where(GenerationJob.status == "QUEUED")
        if job_id is not None:
            statement = statement.where(GenerationJob.id == job_id)
        statement = (
            statement.where(GenerationJob.owner_user_id.not_in(blocked_owners))
            .order_by(GenerationJob.created_at, GenerationJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        result = await db.execute(statement)
        job = result.scalar_one_or_none()
        if job is None:
            await db.commit()
            return None

        if job.attempt_count >= MAX_GENERATION_ATTEMPTS:
            job.status = "EXPIRED"
            job.failure_code = "WORKER_RETRY_LIMIT"
            job.validation_warnings_json = [
                _diagnostic(
                    "WORKER_RETRY_LIMIT",
                    "",
                    "The interrupted generation job reached its retry limit.",
                )
            ]
            job.completed_at = now
            job.updated_at = now
            await db.commit()
            return None
        if job.attempt_count > 0:
            retry_reservations = await db.execute(
                select(
                    func.coalesce(
                        func.sum(
                            GenerationJob.token_budget
                            * func.greatest(GenerationJob.attempt_count, 1)
                        ),
                        0,
                    )
                ).where(
                    GenerationJob.owner_user_id == job.owner_user_id,
                    GenerationJob.created_at >= now - timedelta(hours=24),
                    or_(
                        GenerationJob.status != "CANCELLED",
                        GenerationJob.started_at.is_not(None),
                    ),
                )
            )
            if (
                int(retry_reservations.scalar_one()) + job.token_budget
                > settings.generation_daily_token_budget
            ):
                job.status = "EXPIRED"
                job.failure_code = "RETRY_BUDGET_EXHAUSTED"
                job.validation_warnings_json = [
                    _diagnostic(
                        "RETRY_BUDGET_EXHAUSTED",
                        "",
                        "The interrupted job could not reserve tokens for a retry.",
                    )
                ]
                job.completed_at = now
                job.updated_at = now
                await db.commit()
                return None

        job.attempt_count += 1
        lease_token = uuid.uuid4()
        job.status = "RUNNING"
        job.lease_token = lease_token
        job.lease_expires_at = now + timedelta(
            seconds=settings.generation_job_timeout_seconds + 30
        )
        job.started_at = job.started_at or now
        job.updated_at = now
        await db.commit()
        return ClaimedGenerationJob(
            id=job.id,
            owner_user_id=job.owner_user_id,
            requirement=job.requirement,
            generation_model_id=job.generation_model_id,
            language=job.language,
            token_budget=job.token_budget,
            lease_token=lease_token,
        )


async def run_generation_job_once(
    session_factory: async_sessionmaker[AsyncSession],
    settings: PlatformSettings,
    catalog: ModelCatalog,
    model_client: Any,
    job_id: uuid.UUID,
) -> None:
    """Claim and process a specific job; also used by PostgreSQL regression tests."""
    claimed = await claim_generation_job(session_factory, settings, job_id=job_id)
    if claimed is None:
        return
    await _execute_claimed_job(session_factory, settings, catalog, model_client, claimed)

async def _wait_for_generation_cancel(
    session_factory: async_sessionmaker[AsyncSession],
    settings: PlatformSettings,
    claimed: ClaimedGenerationJob,
) -> None:
    """Wait for cancellation or lease loss in the durable job record."""
    poll_seconds = max(settings.generation_worker_poll_seconds, 0.01)
    while True:
        async with session_factory() as db:
            job = await db.get(GenerationJob, claimed.id)
            if (
                job is None
                or job.lease_token != claimed.lease_token
                or job.status != "RUNNING"
                or job.cancel_requested_at is not None
            ):
                return
        await asyncio.sleep(poll_seconds)


async def _complete_with_cancel_monitor(
    session_factory: async_sessionmaker[AsyncSession],
    settings: PlatformSettings,
    model_client: Any,
    claimed: ClaimedGenerationJob,
    messages: list[dict[str, str]],
) -> ModelCompletion:
    """Abort the provider request when its durable job lease is cancelled."""
    request_task = asyncio.create_task(
        model_client.complete(
            "generation",
            model=claimed.generation_model_id,
            messages=messages,
            max_tokens=claimed.token_budget,
            temperature=settings.generation_llm_temperature,
            reasoning_effort=settings.generation_llm_reasoning_effort,
            max_tokens_parameter=settings.generation_llm_max_tokens_parameter,
        ),
        name=f"generation-provider-{claimed.id}",
    )
    cancel_task = asyncio.create_task(
        _wait_for_generation_cancel(session_factory, settings, claimed),
        name=f"generation-cancel-monitor-{claimed.id}",
    )
    try:
        done, _ = await asyncio.wait(
            (request_task, cancel_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if request_task in done:
            return request_task.result()
        cancel_task.result()
        request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)
        raise asyncio.CancelledError
    finally:
        for task in (request_task, cancel_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(request_task, cancel_task, return_exceptions=True)


async def generation_worker_loop(app: Any) -> None:
    """Run a bounded set of leased durable jobs until application shutdown."""
    session_factory = app.state.session_factory
    settings: PlatformSettings = app.state.settings
    catalog: ModelCatalog = app.state.catalog
    model_client = app.state.model_client
    tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
    app.state.generation_job_tasks = tasks
    try:
        while True:
            async with session_factory() as db:
                now = datetime.now(timezone.utc)
                await _expire_stale_jobs(db, settings, now)
                await db.commit()

            while len(tasks) < settings.generation_max_running_jobs:
                claimed = await claim_generation_job(session_factory, settings)
                if claimed is None:
                    break
                task = asyncio.create_task(
                    _execute_claimed_job(
                        session_factory, settings, catalog, model_client, claimed
                    ),
                    name=f"generation-job-{claimed.id}",
                )
                tasks[claimed.id] = task

            if tasks:
                done, _ = await asyncio.wait(
                    tuple(tasks.values()),
                    timeout=settings.generation_worker_poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for completed in done:
                    job_id = next(
                        key for key, task in tasks.items() if task is completed
                    )
                    del tasks[job_id]
                    try:
                        completed.result()
                    except asyncio.CancelledError:
                        # A user's cancellation stops this child job, not the
                        # application-wide queue worker.
                        continue
                    except Exception as exc:
                        logger.error(
                            "Generation worker task %s escaped with %s",
                            job_id,
                            type(exc).__name__,
                        )
            else:
                await asyncio.sleep(settings.generation_worker_poll_seconds)
    except asyncio.CancelledError:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        raise
    finally:
        app.state.generation_job_tasks = {}


async def _execute_claimed_job(
    session_factory: async_sessionmaker[AsyncSession],
    settings: PlatformSettings,
    catalog: ModelCatalog,
    model_client: Any,
    claimed: ClaimedGenerationJob,
) -> None:
    model_ids = {item.id for item in catalog.generation_models}
    if claimed.generation_model_id not in model_ids:
        await _finish_job(
            session_factory,
            claimed,
            status="FAILED",
            failure_code="GENERATION_MODEL_UNAVAILABLE",
            warnings=[_diagnostic(
                "GENERATION_MODEL_UNAVAILABLE",
                "generation_model_id",
                "The configured generation model is no longer available.",
            )],
        )
        return

    completion: ModelCompletion | None = None
    try:
        messages = _generation_messages(claimed, catalog)
        async with asyncio.timeout(settings.generation_job_timeout_seconds):
            completion = await _complete_with_cancel_monitor(
                session_factory,
                settings,
                model_client,
                claimed,
                messages,
            )
        output = GenerationModelOutput.model_validate_json(completion.content)
        validated = _validate_generation_output(output, catalog)
    except asyncio.CancelledError:
        await _reconcile_interrupted_job(
            session_factory,
            claimed,
            usage=completion.usage if completion is not None else None,
        )
        raise
    except TimeoutError:
        await _finish_job(
            session_factory,
            claimed,
            status="EXPIRED",
            failure_code="MODEL_TIMEOUT",
            warnings=[_diagnostic(
                "MODEL_TIMEOUT", "", "The configured model did not finish within its time limit."
            )],
            usage=completion.usage if completion else None,
        )
        return
    except (ModelClientError, PydanticValidationError, AgentConfigError) as exc:
        if isinstance(exc, ModelClientError):
            code = exc.code
            message = exc.public_message
        else:
            code = "MODEL_OUTPUT_INVALID"
            message = "The configured model did not return a usable proposal."
        await _finish_job(
            session_factory,
            claimed,
            status="FAILED",
            failure_code=code,
            warnings=[_diagnostic(code, "", message)],
            usage=completion.usage if completion else None,
        )
        return
    except Exception as exc:
        logger.error(
            "Generation job %s failed with %s", claimed.id, type(exc).__name__
        )
        await _finish_job(
            session_factory,
            claimed,
            status="FAILED",
            failure_code="GENERATION_INTERNAL_ERROR",
            warnings=[_diagnostic(
                "GENERATION_INTERNAL_ERROR",
                "",
                "Generation failed before a proposal could be validated.",
            )],
            usage=completion.usage if completion else None,
        )
        return

    await _finish_job(
        session_factory,
        claimed,
        status="COMPLETED",
        proposal=validated.proposal,
        unknown_business_facts=validated.unknown_business_facts,
        warnings=validated.validation_warnings,
        usage=completion.usage if completion else None,
    )


async def _finish_job(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedGenerationJob,
    *,
    status: str,
    failure_code: str | None = None,
    proposal: AgentProposal | None = None,
    unknown_business_facts: list[str] | None = None,
    warnings: list[dict[str, str]] | None = None,
    usage: Any = None,
) -> None:
    if status not in JOB_STATUS_VALUES:
        raise ValueError("Unsupported generation job status")
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        result = await db.execute(
            select(GenerationJob)
            .where(
                GenerationJob.id == claimed.id,
                GenerationJob.lease_token == claimed.lease_token,
            )
            .with_for_update()
        )
        job = result.scalar_one_or_none()
        if job is None:
            await db.commit()
            return
        job.usage_json = _combine_usage(
            job.usage_json,
            usage,
            previous_attempts=max(job.attempt_count - 1, 0),
        )
        if (
            job.status in ("CANCELLED", "CANCELLING")
            or job.cancel_requested_at is not None
        ):
            if usage is None:
                job.usage_json = dict(UNKNOWN_USAGE)
            job.status = "CANCELLED"
            job.proposal_json = None
            job.unknown_business_facts_json = []
            job.failure_code = None
            job.completed_at = job.completed_at or now
        elif job.status != "RUNNING":
            await db.commit()
            return
        else:
            job.status = status
            job.proposal_json = (
                proposal.model_dump(mode="json") if proposal is not None else None
            )
            job.unknown_business_facts_json = unknown_business_facts or []
            job.validation_warnings_json = warnings or []
            job.failure_code = failure_code
            job.completed_at = now
        job.lease_token = None
        job.lease_expires_at = None
        job.updated_at = now
        await db.commit()

UNKNOWN_USAGE: dict[str, int | None] = {
    "prompt_tokens": None,
    "completion_tokens": None,
    "total_tokens": None,
}


def _combine_usage(
    previous: dict[str, int | None] | None,
    current: ModelUsage | None,
    *,
    previous_attempts: int,
) -> dict[str, int | None] | None:
    if current is None:
        return previous
    current_usage = current.as_dict()
    if previous is None:
        return dict(UNKNOWN_USAGE) if previous_attempts else current_usage
    if all(value is None for value in previous.values()):
        return dict(UNKNOWN_USAGE)
    return {
        key: (
            previous[key] + current_usage[key]
            if previous.get(key) is not None and current_usage[key] is not None
            else None
        )
        for key in UNKNOWN_USAGE
    }


async def _reconcile_interrupted_job(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedGenerationJob,
    *,
    usage: ModelUsage | None,
) -> None:
    """Keep user cancellations terminal; requeue process interruptions once."""
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        result = await db.execute(
            select(GenerationJob)
            .where(
                GenerationJob.id == claimed.id,
                GenerationJob.lease_token == claimed.lease_token,
            )
            .with_for_update()
        )
        job = result.scalar_one_or_none()
        if job is None:
            await db.commit()
            return
        if usage is None:
            job.usage_json = dict(UNKNOWN_USAGE)
        else:
            job.usage_json = _combine_usage(
                job.usage_json,
                usage,
                previous_attempts=max(job.attempt_count - 1, 0),
            )

        if (
            job.status in ("CANCELLED", "CANCELLING")
            or job.cancel_requested_at is not None
        ):
            job.status = "CANCELLED"
            job.proposal_json = None
            job.unknown_business_facts_json = []
            job.failure_code = None
            job.completed_at = job.completed_at or now
        elif job.status == "RUNNING" and job.attempt_count < MAX_GENERATION_ATTEMPTS:
            job.status = "QUEUED"
            job.proposal_json = None
            job.unknown_business_facts_json = []
            job.validation_warnings_json = []
            job.failure_code = None
            job.completed_at = None
        elif job.status == "RUNNING":
            job.status = "EXPIRED"
            job.failure_code = "WORKER_RETRY_LIMIT"
            job.validation_warnings_json = [
                _diagnostic(
                    "WORKER_RETRY_LIMIT",
                    "",
                    "The interrupted generation job reached its retry limit.",
                )
            ]
            job.completed_at = now
        else:
            await db.commit()
            return

        job.lease_token = None
        job.lease_expires_at = None
        job.updated_at = now
        await db.commit()


async def _expire_stale_jobs(
    db: AsyncSession,
    settings: PlatformSettings,
    now: datetime,
) -> None:
    stale_queue_before = now - timedelta(seconds=settings.generation_job_max_age_seconds)
    await db.execute(
        update(GenerationJob)
        .where(
            GenerationJob.status == "QUEUED",
            GenerationJob.created_at <= stale_queue_before,
        )
        .values(
            status="EXPIRED",
            failure_code="QUEUE_TIMEOUT",
            validation_warnings_json=[
                _diagnostic(
                    "QUEUE_TIMEOUT", "", "The generation job expired before execution."
                )
            ],
            completed_at=now,
            updated_at=now,
        )
    )
    stale_lease = and_(
        GenerationJob.status == "RUNNING",
        GenerationJob.lease_expires_at <= now,
    )
    await db.execute(
        update(GenerationJob)
        .where(stale_lease, GenerationJob.attempt_count < MAX_GENERATION_ATTEMPTS)
        .values(
            status="QUEUED",
            failure_code=None,
            validation_warnings_json=[],
            proposal_json=None,
            unknown_business_facts_json=[],
            usage_json=UNKNOWN_USAGE,
            completed_at=None,
            updated_at=now,
            lease_token=None,
            lease_expires_at=None,
        )
    )
    await db.execute(
        update(GenerationJob)
        .where(stale_lease, GenerationJob.attempt_count >= MAX_GENERATION_ATTEMPTS)
        .values(
            status="EXPIRED",
            failure_code="WORKER_RETRY_LIMIT",
            validation_warnings_json=[
                _diagnostic(
                    "WORKER_RETRY_LIMIT",
                    "",
                    "The interrupted generation job reached its retry limit.",
                )
            ],
            usage_json=UNKNOWN_USAGE,
            completed_at=now,
            updated_at=now,
            lease_token=None,
            lease_expires_at=None,
        )
    )

    await db.execute(
        update(GenerationJob)
        .where(
            GenerationJob.status == "CANCELLING",
            GenerationJob.lease_token.is_not(None),
            GenerationJob.lease_expires_at <= now,
        )
        .values(
            status="CANCELLED",
            proposal_json=None,
            unknown_business_facts_json=[],
            usage_json=UNKNOWN_USAGE,
            completed_at=now,
            updated_at=now,
            lease_token=None,
            lease_expires_at=None,
        )
    )
    await db.execute(
        update(GenerationJob)
        .where(
            GenerationJob.status == "CANCELLED",
            GenerationJob.lease_token.is_not(None),
            GenerationJob.lease_expires_at <= now,
        )
        .values(lease_token=None, lease_expires_at=None, updated_at=now)
    )


def _generation_messages(
    claimed: ClaimedGenerationJob,
    catalog: ModelCatalog,
) -> list[dict[str, str]]:
    allowed = {
        "runtime_models": [item.id for item in catalog.llm_models],
        "speech_providers": [item.id for item in catalog.stt_providers],
        "speech_voices": [item.id for item in catalog.tts_voices],
    }
    system_prompt = (
        "Create one complete, editable agent proposal from the user's requirement. "
        "Return only a JSON object with exactly these fields: name, description, "
        "persona_prompt, config, unknown_business_facts. `config` must be the full "
        "agent authoring envelope with a `flow` object and the platform settings "
        "llm_model, stt_provider, tts_voice_en, and tts_voice_hi. Use only model, "
        "speech-provider, and voice IDs in this allowlist: "
        f"{json.dumps(allowed, ensure_ascii=False, separators=(',', ':'))}. "
        "Use only business facts explicitly present in the requirement. Do not add "
        "prices, rates, eligibility rules, policies, or guarantees unless the user "
        "gave them. List any required but unspecified business facts only in "
        "unknown_business_facts; do not guess values. The requested authoring "
        f"language is {claimed.language}. The flow must have English and Hindi "
        "messages where the schema requires them. Do not publish, call tools, or "
        "claim that a draft was saved. Return no Markdown or surrounding text."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": claimed.requirement},
    ]


def _validate_generation_output(
    output: GenerationModelOutput,
    catalog: ModelCatalog,
) -> ValidatedProposal:
    config = output.config
    proposal_payload = output.model_dump(mode="python")
    secret_errors = secret_diagnostics(proposal_payload)
    structure = validate_draft_structure(config, FlowLimits())
    structural_errors = [item for item in structure if item.severity == "error"]
    if secret_errors or structural_errors:
        diagnostics = [*secret_errors, *structural_errors]
        raise PydanticValidationError.from_exception_data(
            "Generation proposal",
            [
                {
                    "type": "value_error",
                    "loc": (item.path or "config",),
                    "input": None,
                    "ctx": {"error": ValueError(item.code)},
                }
                for item in diagnostics
            ],
        )

    try:
        extract_settings(config)
    except AgentConfigError:
        raise

    catalog_diagnostics = validate_catalog_eligibility(config, catalog)
    semantic_errors, semantic_warnings = validate_draft_semantics(config)
    diagnostics = [*catalog_diagnostics, *semantic_errors, *semantic_warnings]
    adjusted: list[dict[str, str]] = []
    blocking = False
    for diagnostic in diagnostics:
        severity = diagnostic.severity
        if diagnostic.code == "PLAYBACK_UNQUALIFIED":
            severity = "warning"
        elif severity == "error":
            blocking = True
        adjusted.append(
            DiagnosticView(
                code=diagnostic.code,
                path=diagnostic.path,
                message=diagnostic.message,
                severity=severity,
            ).model_dump(mode="json")
        )
    if blocking:
        raise PydanticValidationError.from_exception_data(
            "Generation proposal",
            [
                {
                    "type": "value_error",
                    "loc": (item["path"] or "config",),
                    "input": None,
                    "ctx": {"error": ValueError(item["code"])},
                }
                for item in adjusted
                if item["severity"] == "error"
            ],
        )

    return ValidatedProposal(
        proposal=AgentProposal.model_validate(
            output.model_dump(mode="python", exclude={"unknown_business_facts"})
        ),
        unknown_business_facts=output.unknown_business_facts,
        validation_warnings=adjusted,
    )


def _diagnostic(code: str, path: str, message: str) -> dict[str, str]:
    return {
        "code": code,
        "path": path,
        "message": message,
        "severity": "error",
    }
