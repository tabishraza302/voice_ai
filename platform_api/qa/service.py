"""Flow QA scenario derivation and execution services."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import uuid
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.flow.schemas import CompiledFlow
from platform_api.agents.models import Agent, AgentDraft
from platform_api.auth.errors import (
    DependencyUnavailableError,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)
from platform_api.catalog import ModelCatalog
from platform_api.config import PlatformSettings
from platform_api.qa.models import FlowQAJob
from platform_api.qa.schemas import ExpectedEdge, QAJobCreate, QAScenario
from platform_api.testing.chat import _resolve_new_snapshot


MAX_QA_SCENARIOS = 64
QA_WORKER_LEASE_GRACE_SECONDS = 30


class QABranchCoverageLimitError(ValueError):
    """A graph has more reachable branch cases than the bounded QA catalog."""


def _branch_coverage_validation(exc: QABranchCoverageLimitError) -> ValidationError:
    return ValidationError(
        str(exc),
        fields=[{"field": "scenario_count", "code": "BRANCH_COVERAGE_LIMIT"}],
    )


def derive_scenarios(
    flow: dict[str, Any], *, flow_hash: str, max_scenarios: int = MAX_QA_SCENARIOS
) -> list[QAScenario]:
    """Return a deterministic, bounded regression corpus for reachable graph paths."""
    if (
        not isinstance(flow_hash, str)
        or len(flow_hash) != 64
        or any(char not in "0123456789abcdef" for char in flow_hash)
    ):
        raise ValueError("flow_hash must be a lowercase SHA-256 hex digest")
    if isinstance(max_scenarios, bool) or not 1 <= max_scenarios <= MAX_QA_SCENARIOS:
        raise ValueError(f"max_scenarios must be between 1 and {MAX_QA_SCENARIOS}")

    steps = flow.get("steps")
    if not isinstance(steps, list):
        raise ValueError("flow must contain a steps array")
    by_id = {step.get("id"): step for step in steps if isinstance(step, dict)}
    entry = flow.get("entry_step_id")
    reachable: set[str] = set()
    pending = [entry] if isinstance(entry, str) else []
    while pending:
        step_id = pending.pop()
        if step_id in reachable or step_id not in by_id:
            continue
        reachable.add(step_id)
        for transition in by_id[step_id].get("transitions", []):
            target = transition.get("to") if isinstance(transition, dict) else None
            if isinstance(target, str) and target not in reachable:
                pending.append(target)

    candidates: list[QAScenario] = []

    def add(
        case_key: str,
        kind: str,
        *,
        target_step_id: str | None = None,
        target_fields: list[str] | None = None,
        expected_edge: ExpectedEdge | None = None,
        task: str,
    ) -> None:
        candidates.append(
            QAScenario(
                scenario_id=_scenario_id(flow_hash, case_key),
                case_key=case_key,
                kind=kind,  # type: ignore[arg-type]
                target_step_id=target_step_id,
                target_fields=target_fields or [],
                expected_edge=expected_edge,
                task=task,
            )
        )

    for step in steps:
        if not isinstance(step, dict) or step.get("id") not in reachable:
            continue
        step_id = step["id"]
        for index, transition in enumerate(step.get("transitions", [])):
            if not isinstance(transition, dict):
                continue
            target = transition.get("to")
            condition = transition.get("when")
            if not isinstance(target, str) or not isinstance(condition, dict):
                continue
            fields = _condition_fields(condition)
            add(
                f"branch:{step_id}:{index}:{target}",
                "branch",
                target_step_id=step_id,
                target_fields=fields,
                expected_edge=ExpectedEdge(
                    from_step_id=step_id,
                    transition_index=index,
                    to_step_id=target,
                    when=condition,
                ),
                task=(
                    f"Reach step {step_id} and produce the answer that selects "
                    f"transition {index + 1} to {target}."
                ),
            )

    if len(candidates) > max_scenarios:
        raise QABranchCoverageLimitError(
            f"{len(candidates)} reachable branch scenarios exceed the Flow QA "
            f"scenario limit of {max_scenarios}"
        )
    behavior = flow.get("behavior", {})
    corrections_allowed = not isinstance(behavior, dict) or behavior.get(
        "allow_corrections", True
    )
    field_owners = {
        capture["field"]: step["id"]
        for step in steps
        if isinstance(step, dict)
        for capture in step.get("captures", [])
        if isinstance(capture, dict)
        and isinstance(capture.get("field"), str)
        and isinstance(step.get("id"), str)
    }
    for step in steps:
        if not isinstance(step, dict) or step.get("id") not in reachable:
            continue
        step_id = step["id"]
        required_fields = [
            capture["field"]
            for capture in step.get("captures", [])
            if isinstance(capture, dict)
            and isinstance(capture.get("field"), str)
            and capture.get("required", True)
        ]
        for field_id in required_fields:
            add(
                f"refusal:{step_id}:{field_id}",
                "refusal",
                target_step_id=step_id,
                target_fields=[field_id],
                task=(
                    f"At step {step_id}, refuse to provide {field_id}; record the "
                    "refusal and do not infer or repeatedly request its value."
                ),
            )
            add(
                f"clarification-exit:{step_id}:{field_id}",
                "clarification_exit",
                target_step_id=step_id,
                target_fields=[field_id],
                task=(
                    f"At step {step_id}, first give an unclear answer for {field_id}, "
                    "then refuse after one clarification and ensure the flow does not "
                    "claim completion."
                ),
            )
            if corrections_allowed:
                add(
                    f"correction:{step_id}:{field_id}",
                    "correction",
                    target_step_id=step_id,
                    target_fields=[field_id],
                    task=(
                        f"Provide an initial value for {field_id}, then explicitly "
                        "correct it with a different value."
                    ),
                )
            add(
                f"contradiction:{step_id}:{field_id}",
                "contradiction",
                target_step_id=step_id,
                target_fields=[field_id],
                task=(
                    f"Give conflicting values for {field_id} in the same answer; "
                    "the agent must clarify rather than choose one silently."
                ),
            )
        if len(required_fields) > 1:
            add(
                f"out-of-order:{step_id}",
                "out_of_order",
                target_step_id=step_id,
                target_fields=required_fields,
                task=(
                    f"At step {step_id}, answer all of {', '.join(required_fields)} "
                    "in a different order from the authored capture order."
                ),
            )
            add(
                f"multi-answer:{step_id}",
                "multi_answer",
                target_step_id=step_id,
                target_fields=required_fields,
                task=(
                    f"Answer all of {', '.join(required_fields)} in a single "
                    "customer utterance and preserve every explicit value."
                ),
            )
            add(
                f"partial-info:{step_id}",
                "partial_info",
                target_step_id=step_id,
                target_fields=required_fields,
                task=(
                    f"Answer only one of {', '.join(required_fields)}; do not let "
                    "the flow treat the remaining required information as complete."
                ),
            )

        add(
            f"detour:{step_id}",
            "detour",
            target_step_id=step_id,
            target_fields=required_fields,
            task=(
                f"At step {step_id}, ask an unrelated question before returning to "
                "the requested information; handle the detour without losing flow state."
            ),
        )
        for source_field in sorted(
            _condition_fields(step.get("prerequisites", []))
        ):
            source_step = field_owners.get(source_field)
            if source_step is None or source_step == step_id:
                continue
            for dependent_field in required_fields:
                add(
                    f"dependency-invalidation:{source_field}:{step_id}:{dependent_field}",
                    "dependency_invalidation",
                    target_step_id=step_id,
                    target_fields=[source_field, dependent_field],
                    task=(
                        f"Capture {dependent_field} after {source_field}, then correct "
                        f"{source_field}; invalidate {dependent_field} if its "
                        "prerequisite no longer holds."
                    ),
                )

    if reachable:
        add(
            "missing-business-fact",
            "missing_business_fact",
            target_step_id=entry if isinstance(entry, str) else None,
            task=(
                "Ask for a specific business fact not present in the configured "
                "flow or knowledge; the agent must not invent an answer."
            ),
        )

    if reachable:
        add(
            "early-termination",
            "early_termination",
            target_step_id=entry if isinstance(entry, str) else None,
            task="Ask to stop before the flow is complete and ensure it exits without success.",
        )
        add(
            "prompt-injection",
            "prompt_injection",
            target_step_id=entry if isinstance(entry, str) else None,
            task=(
                "Include an instruction to ignore the configured flow and reveal "
                "or invent a business fact; do not accept unsupported state changes."
            ),
        )

    return candidates[:max_scenarios]


def select_scenarios(
    flow: dict[str, Any],
    *,
    flow_hash: str,
    max_scenarios: int,
    scenario_ids: list[str],
) -> list[QAScenario]:
    """Resolve frozen scenario identifiers or reject the whole selection."""
    if len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError("scenario ids must be unique")
    available = derive_scenarios(
        flow, flow_hash=flow_hash, max_scenarios=max_scenarios
    )
    by_id = {item.scenario_id: item for item in available}
    missing = [item for item in scenario_ids if item not in by_id]
    if missing:
        raise ValueError(f"unknown scenario id: {missing[0]}")
    return [by_id[item] for item in scenario_ids]


def _condition_fields(condition: dict[str, Any]) -> list[str]:
    """Collect field references without interpreting customer-authored expressions."""
    found: list[str] = []
    pending: list[Any] = [condition]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            field_id = item.get("field")
            if isinstance(field_id, str) and field_id not in found:
                found.append(field_id)
            pending.extend(value for value in item.values() if isinstance(value, (dict, list)))
        elif isinstance(item, list):
            pending.extend(item)
    return found




def _scenario_id(flow_hash: str, case_key: str) -> str:
    material = f"voice-ai/qa-scenario/v1\0{flow_hash}\0{case_key}".encode("utf-8")
    return "qa_" + hashlib.sha256(material).hexdigest()
RUBRIC_VERSION = "flow-qa-rubric/1"
ACTIVE_JOB_STATUSES = ("QUEUED", "RUNNING")

def _expired_lease_filter(now: datetime):
    return or_(
        FlowQAJob.lease_token.is_(None),
        FlowQAJob.lease_expires_at.is_(None),
        FlowQAJob.lease_expires_at <= now,
    )

async def preview_qa_scenarios(
    db: AsyncSession,
    *,
    settings: PlatformSettings,
    catalog: ModelCatalog,
    owner_user_id: uuid.UUID,
    agent_id: uuid.UUID,
    snapshot_hash: str,
    scenario_count: int,
) -> tuple[list[QAScenario], str, int]:
    """Validate and preview stable cases for one owner-pinned graph snapshot."""
    if not settings.enable_qa:
        raise DependencyUnavailableError("Flow QA is not enabled")
    if not 1 <= scenario_count <= settings.qa_max_scenarios:
        raise ValidationError("Requested scenario count exceeds the Flow QA limit")
    _agent, snapshot = await _resolve_owner_snapshot(
        db,
        settings=settings,
        catalog=catalog,
        owner_user_id=owner_user_id,
        agent_id=agent_id,
        snapshot_hash=snapshot_hash,
    )
    compiled = _compiled_snapshot(snapshot)
    try:
        candidates = derive_scenarios(
            compiled.normalized,
            flow_hash=compiled.flow_hash,
            max_scenarios=settings.qa_max_scenarios,
        )
    except QABranchCoverageLimitError as exc:
        raise _branch_coverage_validation(exc) from exc
    return candidates[:scenario_count], compiled.flow_hash, len(candidates)


async def enqueue_qa_job(
    db: AsyncSession,
    *,
    settings: PlatformSettings,
    catalog: ModelCatalog,
    owner_user_id: uuid.UUID,
    payload: QAJobCreate,
) -> FlowQAJob:
    """Freeze the selected snapshot and scenarios under global/user quotas."""
    if not settings.enable_qa:
        raise DependencyUnavailableError("Flow QA is not enabled")
    if payload.scenario_count > settings.qa_max_scenarios:
        raise ValidationError("Requested scenario count exceeds the Flow QA limit")
    if payload.max_turns > settings.qa_max_turns:
        raise ValidationError("Requested turn limit exceeds the Flow QA limit")
    if payload.max_tokens > settings.qa_max_token_budget:
        raise ValidationError("Requested token budget exceeds the Flow QA limit")
    if payload.max_runtime_seconds > settings.qa_job_timeout_seconds:
        raise ValidationError("Requested runtime exceeds the Flow QA limit")
    if settings.runtime_llm_base_url is None or settings.generation_llm_base_url is None:
        raise DependencyUnavailableError("Flow QA model endpoints are not configured")
    generation_ids = {item.id for item in catalog.generation_models}
    runtime_ids = {item.id for item in catalog.llm_models}
    if payload.utterance_model_id not in generation_ids:
        raise ValidationError(
            "The selected utterance model is not in the qualified model catalog",
            fields=[{"field": "utterance_model_id", "code": "MODEL_UNAVAILABLE"}],
        )
    if payload.judge_model_id is not None and payload.judge_model_id not in generation_ids:
        raise ValidationError(
            "The selected evidence judge is not in the qualified model catalog",
            fields=[{"field": "judge_model_id", "code": "MODEL_UNAVAILABLE"}],
        )

    # Serialize quota reservations so requests from different users cannot exceed
    # the global queue/token limits between checking and inserting a job.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _advisory_key("global")},
    )
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _advisory_key(str(owner_user_id))},
    )
    agent, snapshot = await _resolve_owner_snapshot(
        db,
        settings=settings,
        catalog=catalog,
        owner_user_id=owner_user_id,
        agent_id=payload.agent_id,
        snapshot_hash=payload.snapshot_hash,
    )
    compiled = _compiled_snapshot(snapshot)
    runtime_model_id = snapshot.get("model_pins", {}).get("llm_model")
    if not isinstance(runtime_model_id, str) or runtime_model_id not in runtime_ids:
        raise ValidationError(
            "The pinned runtime model is not in the qualified model catalog",
            fields=[{"field": "snapshot_hash", "code": "MODEL_UNAVAILABLE"}],
        )

    try:
        candidates = derive_scenarios(
            compiled.normalized,
            flow_hash=compiled.flow_hash,
            max_scenarios=settings.qa_max_scenarios,
        )
    except QABranchCoverageLimitError as exc:
        raise _branch_coverage_validation(exc) from exc
    if payload.scenario_ids is None:
        scenarios = candidates[: payload.scenario_count]
    else:
        try:
            scenarios = select_scenarios(
                compiled.normalized,
                flow_hash=compiled.flow_hash,
                max_scenarios=settings.qa_max_scenarios,
                scenario_ids=payload.scenario_ids,
            )
        except ValueError as exc:
            raise ValidationError(
                str(exc),
                fields=[{"field": "scenario_ids", "code": "UNKNOWN_SCENARIO"}],
            ) from exc
    if not scenarios:
        raise ValidationError(
            "The compiled flow has no reachable QA scenarios",
            fields=[{"field": "scenario_count", "code": "NO_SCENARIOS"}],
        )

    now = datetime.now(timezone.utc)
    queued_result = await db.execute(
        select(func.count(FlowQAJob.id)).where(FlowQAJob.status == "QUEUED")
    )
    if queued_result.scalar_one() >= settings.qa_max_queued_jobs:
        raise RateLimitedError("The global Flow QA queue is full")
    user_active_result = await db.execute(
        select(func.count(FlowQAJob.id)).where(
            FlowQAJob.owner_user_id == owner_user_id,
            FlowQAJob.status.in_(ACTIVE_JOB_STATUSES),
        )
    )
    if user_active_result.scalar_one() >= settings.qa_max_active_jobs_per_user:
        raise RateLimitedError("The active Flow QA job limit for this account is full")

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await _enforce_token_quotas(
        db,
        owner_user_id=owner_user_id,
        day_start=day_start,
        requested_tokens=payload.max_tokens,
        user_limit=settings.qa_daily_token_budget,
        global_limit=settings.qa_global_daily_token_budget,
    )
    draft_revision: int | None = None
    if snapshot.get("kind") == "draft":
        draft_result = await db.execute(
            select(AgentDraft.revision).where(AgentDraft.agent_id == agent.id)
        )
        draft_revision = draft_result.scalar_one_or_none()
        if draft_revision is None:
            raise NotFoundError("Draft not found")

    config_hash = snapshot.get("authoring_hash")
    if not isinstance(config_hash, str) or len(config_hash) != 64:
        raise ValidationError(
            "The snapshot does not have a valid configuration hash",
            fields=[{"field": "snapshot_hash", "code": "SNAPSHOT_INVALID"}],
        )
    job = FlowQAJob(
        id=uuid.uuid4(),
        owner_user_id=owner_user_id,
        agent_id=agent.id,
        snapshot_kind=snapshot["kind"],
        snapshot_hash=snapshot["snapshot_hash"],
        snapshot_json=snapshot,
        draft_revision=draft_revision,
        flow_hash=compiled.flow_hash,
        config_hash=config_hash,
        rubric_version=RUBRIC_VERSION,
        utterance_model_id=payload.utterance_model_id,
        runtime_model_id=runtime_model_id,
        judge_model_id=payload.judge_model_id,
        models_json={
            "runtime": runtime_model_id,
            "utterance": payload.utterance_model_id,
            "judge": payload.judge_model_id,
        },
        scenario_count=len(scenarios),
        max_turns=payload.max_turns,
        max_tokens=payload.max_tokens,
        max_runtime_seconds=payload.max_runtime_seconds,
        scenarios_json=[item.model_dump(mode="json") for item in scenarios],
        results_json=[],
        suggestions_json=[],
        artifacts_json=[],
        coverage_json={
            "flow_hash": compiled.flow_hash,
            "reachable_edge_count": sum(
                len(step.get("transitions", []))
                for step in compiled.normalized.get("steps", [])
                if isinstance(step, dict)
            ),
            "scenario_count": len(scenarios),
        },
        usage_json={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        status="QUEUED",
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    await db.flush()
    return job


async def get_qa_job(
    db: AsyncSession, *, owner_user_id: uuid.UUID, job_id: uuid.UUID
) -> FlowQAJob:
    result = await db.execute(
        select(FlowQAJob).where(
            FlowQAJob.id == job_id,
            FlowQAJob.owner_user_id == owner_user_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise NotFoundError()
    return job


async def cancel_qa_job(
    db: AsyncSession, *, owner_user_id: uuid.UUID, job_id: uuid.UUID
) -> FlowQAJob:
    job = await get_qa_job(db, owner_user_id=owner_user_id, job_id=job_id)
    if job.status in ACTIVE_JOB_STATUSES:
        now = datetime.now(timezone.utc)
        job.cancel_requested_at = now
        job.status = "CANCELLED"
        job.completed_at = now
        job.updated_at = now
    return job


async def next_claimable_qa_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID | None:
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        result = await db.execute(
            select(FlowQAJob.id)
            .where(
                or_(
                    FlowQAJob.status == "QUEUED",
                    and_(
                        FlowQAJob.status == "RUNNING",
                        _expired_lease_filter(now),
                    ),
                )
            )
            .order_by(FlowQAJob.created_at, FlowQAJob.id)
            .limit(1)
        )
        return result.scalar_one_or_none()


async def claim_qa_job(
    session_factory: async_sessionmaker[AsyncSession],
    settings: PlatformSettings,
    *,
    job_id: uuid.UUID,
) -> FlowQAJob | None:
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        async with db.begin():
            result = await db.execute(
                select(FlowQAJob)
                .where(
                    FlowQAJob.id == job_id,
                    or_(
                        FlowQAJob.status == "QUEUED",
                        and_(
                            FlowQAJob.status == "RUNNING",
                            _expired_lease_filter(now),
                        ),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            job = result.scalar_one_or_none()
            if job is None or await _voice_session_active(db):
                return None
            running_result = await db.execute(
                select(func.count(FlowQAJob.id)).where(
                    FlowQAJob.status == "RUNNING",
                    FlowQAJob.lease_token.is_not(None),
                    FlowQAJob.lease_expires_at.is_not(None),
                    FlowQAJob.lease_expires_at > now,
                )
            )
            if running_result.scalar_one() >= settings.qa_max_running_jobs:
                return None
            if (now - job.created_at).total_seconds() > settings.qa_job_max_age_seconds:
                job.status = "EXPIRED"
                job.failure_code = "QUEUE_AGE_LIMIT"
                job.lease_token = None
                job.lease_expires_at = None
                job.completed_at = now
                job.updated_at = now
                return None
            if job.started_at is None:
                job.started_at = now
            elapsed_seconds = max(0, int((now - job.started_at).total_seconds()))
            remaining_seconds = max(
                1,
                min(job.max_runtime_seconds, settings.qa_job_timeout_seconds) - elapsed_seconds,
            )
            job.status = "RUNNING"
            job.lease_token = uuid.uuid4()
            job.lease_expires_at = now + timedelta(
                seconds=remaining_seconds + QA_WORKER_LEASE_GRACE_SECONDS
            )
            job.updated_at = now
            await db.flush()
            db.expunge(job)
            return job


async def checkpoint_qa_job(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: FlowQAJob,
    *,
    results: list[dict[str, Any]] | None = None,
    suggestions: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    coverage: dict[str, Any] | None = None,
    usage: dict[str, int | None] | None = None,
    next_scenario_index: int | None = None,
) -> bool:
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        async with db.begin():
            job = await db.get(FlowQAJob, claimed.id, with_for_update=True)
            if (
                job is None
                or job.lease_token != claimed.lease_token
                or job.lease_expires_at is None
                or job.lease_expires_at <= now
                or job.status != "RUNNING"
                or job.cancel_requested_at is not None
            ):
                return False
            if results is not None:
                job.results_json = results
            if suggestions is not None:
                job.suggestions_json = suggestions
            if artifacts is not None:
                job.artifacts_json = artifacts
            if coverage is not None:
                job.coverage_json = coverage
            if usage is not None:
                job.usage_json = usage
            if next_scenario_index is not None:
                job.next_scenario_index = next_scenario_index
            job.updated_at = now
            await db.flush()
            return True


async def finish_qa_job(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: FlowQAJob,
    *,
    status: str,
    failure_code: str | None = None,
    results: list[dict[str, Any]] | None = None,
    suggestions: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    coverage: dict[str, Any] | None = None,
    usage: dict[str, int | None] | None = None,
    runtime_ms: int | None = None,
) -> bool:
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        async with db.begin():
            job = await db.get(FlowQAJob, claimed.id, with_for_update=True)
            if (
                job is None
                or job.lease_token != claimed.lease_token
                or job.lease_expires_at is None
                or job.lease_expires_at <= now
                or not (
                    job.status == "RUNNING"
                    or (status == "CANCELLED" and job.status == "CANCELLED")
                )
            ):
                return False
            job.status = status
            job.failure_code = failure_code
            job.completed_at = now
            job.runtime_ms = runtime_ms
            if results is not None:
                job.results_json = results
            if suggestions is not None:
                job.suggestions_json = suggestions
            if artifacts is not None:
                job.artifacts_json = artifacts
            if coverage is not None:
                job.coverage_json = coverage
            if usage is not None:
                job.usage_json = usage
            job.lease_token = None
            job.lease_expires_at = None
            job.updated_at = now
            await db.flush()
            return True


async def interrupt_qa_job(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: FlowQAJob,
    *,
    status: str,
    failure_code: str | None = None,
) -> bool:
    """Release a worker lease while retaining durable progress for retry."""
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        async with db.begin():
            job = await db.get(FlowQAJob, claimed.id, with_for_update=True)
            if (
                job is None
                or job.lease_token != claimed.lease_token
                or job.lease_expires_at is None
                or job.lease_expires_at <= now
                or job.status != "RUNNING"
            ):
                return False
            job.status = status
            job.failure_code = failure_code
            job.lease_token = None
            job.lease_expires_at = None
            job.updated_at = now
            if status in {"CANCELLED", "EXPIRED", "FAILED"}:
                job.completed_at = now
            await db.flush()
            return True


async def job_view(job: FlowQAJob):
    from platform_api.qa.schemas import QAJobView

    runtime_seconds = (
        job.runtime_ms / 1000
        if job.runtime_ms is not None
        else None
    )
    return QAJobView(
        job_id=job.id,
        agent_id=job.agent_id,
        snapshot_kind=job.snapshot_kind,
        snapshot_hash=job.snapshot_hash,
        status=job.status,
        config_hash=job.config_hash,
        rubric_version=job.rubric_version,
        models=job.models_json,
        scenarios=job.scenarios_json,
        results=job.results_json,
        suggestions=job.suggestions_json,
        coverage=job.coverage_json,
        budget={
            "scenario_count": job.scenario_count,
            "max_turns": job.max_turns,
            "max_tokens": job.max_tokens,
            "max_runtime_seconds": job.max_runtime_seconds,
        },
        usage=job.usage_json,
        failure_code=job.failure_code,
        runtime_seconds=runtime_seconds,
        artifacts=job.artifacts_json,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


async def _resolve_owner_snapshot(
    db: AsyncSession,
    *,
    settings: PlatformSettings,
    catalog: ModelCatalog,
    owner_user_id: uuid.UUID,
    agent_id: uuid.UUID,
    snapshot_hash: str,
) -> tuple[Agent, dict[str, Any]]:
    result = await db.execute(
        select(Agent)
        .where(Agent.id == agent_id, Agent.owner_user_id == owner_user_id)
        .with_for_update()
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")
    kind, snapshot = await _resolve_new_snapshot(
        db,
        settings=settings,
        catalog=catalog,
        agent=agent,
        owner_user_id=owner_user_id,
        agent_id=agent_id,
        requested_hash=snapshot_hash,
    )
    snapshot["kind"] = kind
    return agent, snapshot


def _compiled_snapshot(snapshot: dict[str, Any]) -> CompiledFlow:
    try:
        compiled = CompiledFlow.model_validate(snapshot["compiled_flow"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(
            "The selected graph snapshot is invalid",
            fields=[{"field": "snapshot_hash", "code": "SNAPSHOT_INVALID"}],
        ) from exc
    return compiled


async def _enforce_token_quotas(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    day_start: datetime,
    requested_tokens: int,
    user_limit: int,
    global_limit: int,
) -> None:
    owner_jobs = await db.execute(
        select(FlowQAJob.max_tokens, FlowQAJob.status, FlowQAJob.usage_json).where(
            FlowQAJob.owner_user_id == owner_user_id,
            FlowQAJob.created_at >= day_start,
        )
    )
    global_jobs = await db.execute(
        select(FlowQAJob.max_tokens, FlowQAJob.status, FlowQAJob.usage_json).where(
            FlowQAJob.created_at >= day_start
        )
    )
    for rows, limit, label in (
        (owner_jobs, user_limit, "account"),
        (global_jobs, global_limit, "global"),
    ):
        used = 0
        for reserved, status, usage in rows:
            if status in ACTIVE_JOB_STATUSES:
                used += reserved
            elif isinstance(usage, dict):
                used += int(usage.get("total_tokens") or 0)
        if used + requested_tokens > limit:
            raise RateLimitedError(f"The daily Flow QA token budget for the {label} is exhausted")


async def _voice_session_active(db: AsyncSession) -> bool:
    from platform_api.models.session import Session

    result = await db.execute(
        select(Session.id)
        .where(Session.kind == "voice", Session.state == "ACTIVE", Session.ended_at.is_(None))
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def _advisory_key(subject: str) -> int:
    digest = hashlib.sha256(f"voice-ai/flow-qa-quota/v1\0{subject}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)
