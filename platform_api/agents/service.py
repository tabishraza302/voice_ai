"""Platform service layer for agent CRUD, mutable draft CAS, templates, and idempotency.

Follows master spec III.3 & III.5 and flow-contract.md:
- Owner-isolated agent lifecycle (CRUD, enable, disable, duplicate)
- Compare-and-swap (CAS) draft revisions
- Validation endpoints (RFC 6901 pointers, catalog eligibility, semantics)
- Platform-owned template creation copying flow into an initial draft
- Version history, immutable reads, and draft restoration
- Request-level idempotency journal for mutating actions
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
from typing import Any
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.flow.canonical import (
    canonical_json_v1,
    compute_authoring_hash,
    compute_flow_hash,
)
from bot.flow.qualification import AgentQualificationSettings
from bot.flow.schemas import AdapterCapabilities, FlowLimits
from bot.flow.validation import normalize_executable
from platform_api.agents.config import (
    AgentConfigError,
    extract_flow,
    validate_config_bounds,
)
from platform_api.agents.defaults import DEFAULT_AGENT_TEMPLATES
from platform_api.agents.models import (
    Agent,
    AgentDraft,
    AgentTemplateVersion,
    AgentVersion,
    IdempotencyRecord,
)
from platform_api.agents.publishing import publish_agent_version
from platform_api.agents.schemas import (
    AgentCreate,
    AgentListResponse,
    AgentUpdate,
    AgentView,
    DraftUpdate,
    DraftView,
    DuplicateAgentRequest,
    PublishRequest,
    RestoreDraftRequest,
    TemplateDetailView,
    TemplateListResponse,
    TemplateView,
    ValidateRequest,
    ValidationResponse,
    ValidationSummaryView,
    VersionDetailView,
    VersionListResponse,
    VersionView,
)
from platform_api.agents.security import find_secret_paths
from platform_api.agents.validation import (
    diagnostics_to_views,
    secret_diagnostics,
    validate_catalog_eligibility,
    validate_draft_semantics,
    validate_draft_structure,
)
from platform_api.audit import record_audit
from platform_api.auth.errors import (
    AuthoringHashMismatchError,
    IdempotencyConflictError,
    NotFoundError,
    RevisionConflictError,
    ValidationError,
)
from platform_api.catalog import ModelCatalog

DEFAULT_EMPTY_FLOW: dict[str, Any] = {
    "schema_version": 1,
    "steps": [],
}


# --- Idempotency Helpers ------------------------------------------------------


def compute_payload_hash(payload_dict: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash over canonical JSON representation."""
    canonical_bytes = canonical_json_v1(payload_dict)
    return hashlib.sha256(canonical_bytes).hexdigest()


async def check_idempotency_record(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    idempotency_key: str,
    action: str,
    request_hash: str,
) -> IdempotencyRecord | None:
    """Check if an idempotency record exists for (user_id, idempotency_key, action).

    Raises IdempotencyConflictError if key was reused with a different payload.
    """
    stmt = select(IdempotencyRecord).where(
        IdempotencyRecord.user_id == user_id,
        IdempotencyRecord.idempotency_key == idempotency_key,
        IdempotencyRecord.action == action,
    )
    res = await db.execute(stmt)
    record = res.scalar_one_or_none()
    if record is not None:
        if record.request_hash != request_hash:
            raise IdempotencyConflictError(
                f"Idempotency key '{idempotency_key}' reused with different request payload"
            )
        return record
    return None


async def lock_idempotency_key(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    idempotency_key: str,
    action: str,
) -> None:
    """Serialize a key/action scope until the current PostgreSQL transaction ends."""
    lock_material = (
        b"voice-ai/idempotency-lock/v1\0"
        + user_id.bytes
        + b"\0"
        + action.encode("utf-8")
        + b"\0"
        + idempotency_key.encode("utf-8")
    )
    digest = hashlib.sha256(lock_material).digest()
    lock_part_1 = int.from_bytes(digest[:4], "big", signed=True)
    lock_part_2 = int.from_bytes(digest[4:8], "big", signed=True)
    await db.execute(
        select(func.pg_advisory_xact_lock(lock_part_1, lock_part_2))
    )


async def store_idempotency_record(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    idempotency_key: str,
    action: str,
    request_hash: str,
    status_code: int,
    response_json: dict[str, Any],
) -> IdempotencyRecord:
    """Persist outcome in idempotency journal within active transaction."""
    record = IdempotencyRecord(
        id=uuid.uuid4(),
        user_id=user_id,
        idempotency_key=idempotency_key,
        action=action,
        request_hash=request_hash,
        status_code=status_code,
        response_headers={},
        response_json=response_json,
        created_at=datetime.now(timezone.utc),
    )
    db.add(record)
    await db.flush()
    return record


# --- Validation helpers -------------------------------------------------------


def _validation_summary(
    config: dict[str, Any],
    catalog: ModelCatalog | None = None,
    capabilities: AdapterCapabilities | None = None,
    qualification_settings: AgentQualificationSettings | None = None,
) -> ValidationSummaryView:
    """Return the live draft validation summary, including secret/catalog errors."""
    secret_errors = secret_diagnostics(config)
    structural = validate_draft_structure(config)
    structural_errors = [d for d in structural if d.severity == "error"]
    if structural_errors:
        return ValidationSummaryView(
            errors=diagnostics_to_views(secret_errors + structural_errors),
            warnings=[],
        )

    catalog_errors = validate_catalog_eligibility(config, catalog)
    semantic_errors, warnings = validate_draft_semantics(
        config,
        capabilities=capabilities,
        qualification_settings=qualification_settings,
    )
    return ValidationSummaryView(
        errors=diagnostics_to_views(
            secret_errors + catalog_errors + semantic_errors
        ),
        warnings=diagnostics_to_views(warnings),
    )


def _raise_for_secret_keys(config: dict[str, Any]) -> None:
    paths = find_secret_paths(config)
    if paths:
        raise ValidationError(
            "Agent configuration contains forbidden secret fields",
            fields=[
                {"field": path, "code": "SECRET_NOT_ALLOWED"} for path in paths
            ],
        )


def _config_error(exc: AgentConfigError) -> ValidationError:
    return ValidationError(
        "Agent configuration envelope is invalid",
        fields=[{"field": exc.path, "code": "SCHEMA_INVALID"}],
    )


# --- Agent CRUD ---------------------------------------------------------------


async def list_agents(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 20,
) -> AgentListResponse:
    """List agents owned by requesting user with cursor pagination."""
    clamped_limit = max(1, min(limit, 100))
    stmt = (
        select(Agent)
        .where(Agent.owner_user_id == owner_user_id)
        .order_by(Agent.created_at.desc(), Agent.id.desc())
        .limit(clamped_limit + 1)
    )
    if cursor:
        try:
            cursor_dt_str, cursor_id_str = cursor.split("_", 1)
            # Query-string decoding turns an unescaped '+' offset into a space.
            cursor_dt = datetime.fromisoformat(cursor_dt_str.replace(" ", "+"))
            cursor_id = uuid.UUID(cursor_id_str)
            stmt = stmt.where(
                (Agent.created_at < cursor_dt)
                | ((Agent.created_at == cursor_dt) & (Agent.id < cursor_id))
            )
        except Exception:
            pass

    res = await db.execute(stmt)
    agents = list(res.scalars().all())

    next_cursor = None
    if len(agents) > clamped_limit:
        agents.pop()
        last = agents[-1]
        # Keep the cursor URL-safe: '+' in an ISO offset is decoded as a space.
        next_cursor = f"{last.created_at.isoformat().replace('+00:00', 'Z')}_{last.id}"

    views = [AgentView.model_validate(a) for a in agents]
    return AgentListResponse(items=views, next_cursor=next_cursor)


async def get_agent(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> Agent:
    """Get agent by ID strictly scoped to owner. Raises 404 if not found."""
    stmt = select(Agent).where(
        Agent.id == agent_id,
        Agent.owner_user_id == owner_user_id,
    )
    res = await db.execute(stmt)
    agent = res.scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")
    return agent


async def create_agent(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    payload: AgentCreate,
    session_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    source_ip: str | None = None,
) -> AgentView:
    """Create a new agent and initial draft for owner."""
    now = datetime.now(timezone.utc)

    if payload.source == "template" and payload.template_version_id is None:
        raise ValidationError(
            "template_version_id is required when source is 'template'",
            fields=[{"field": "template_version_id", "code": "REQUIRED"}],
        )

    # 1. Determine template / flow to copy. Platform defaults are seeded
    # lazily so a client can create from a known template without listing first.
    authoring_flow = copy.deepcopy(DEFAULT_EMPTY_FLOW)
    source = payload.source
    template_version_id = None

    if payload.template_version_id is not None:
        await ensure_default_templates(db)
        tpl_stmt = select(AgentTemplateVersion).where(
            AgentTemplateVersion.id == payload.template_version_id,
            AgentTemplateVersion.is_active == True,  # noqa: E712
        )
        tpl_res = await db.execute(tpl_stmt)
        template = tpl_res.scalar_one_or_none()
        if template is None:
            raise NotFoundError("Agent template not found")
        authoring_flow = copy.deepcopy(template.flow_json)
        source = "template"
        template_version_id = template.id

    try:
        validate_config_bounds(authoring_flow, FlowLimits().max_flow_bytes)
    except AgentConfigError as exc:
        raise _config_error(exc) from exc
    authoring_hash = compute_authoring_hash(authoring_flow)

    # 2. Persist Agent entity.
    agent = Agent(
        id=uuid.uuid4(),
        owner_user_id=owner_user_id,
        name=payload.name,
        description=payload.description,
        persona_prompt=payload.persona_prompt,
        enabled=False,
        draft_revision=1,
        current_published_version_id=None,
        created_at=now,
        updated_at=now,
    )
    db.add(agent)
    await db.flush()

    # 3. Persist AgentDraft entity.
    draft = AgentDraft(
        agent_id=agent.id,
        authoring_json=authoring_flow,
        authoring_hash=authoring_hash,
        revision=1,
        source=source,
        template_version_id=template_version_id,
        updated_at=now,
    )
    db.add(draft)
    await db.flush()

    # 4. Record audit log.
    await record_audit(
        db,
        actor_kind="user",
        actor_id=owner_user_id,
        session_id=session_id,
        action="agent.create",
        target_type="agent",
        target_id=agent.id,
        result="success",
        request_id=request_id,
        source_ip=source_ip,
        change_summary={
            "agent_id": str(agent.id),
            **(
                {"template_version_id": str(template_version_id)}
                if template_version_id is not None
                else {}
            ),
        },
        created_at=now,
    )

    return AgentView.model_validate(agent)


async def update_agent(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    payload: AgentUpdate,
    session_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    source_ip: str | None = None,
) -> AgentView:
    """Update agent metadata (name, description) scoped to owner."""
    now = datetime.now(timezone.utc)
    stmt = (
        select(Agent)
        .where(Agent.id == agent_id, Agent.owner_user_id == owner_user_id)
        .with_for_update()
    )
    res = await db.execute(stmt)
    agent = res.scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")

    changed_fields: list[str] = []
    if payload.name is not None:
        agent.name = payload.name
        changed_fields.append("name")
    if payload.description is not None:
        agent.description = payload.description
        changed_fields.append("description")
    if payload.persona_prompt is not None:
        agent.persona_prompt = payload.persona_prompt
        changed_fields.append("persona_prompt")

    if changed_fields:
        agent.updated_at = now
        await db.flush()
        await record_audit(
            db,
            actor_kind="user",
            actor_id=owner_user_id,
            session_id=session_id,
            action="agent.update",
            target_type="agent",
            target_id=agent.id,
            result="success",
            request_id=request_id,
            source_ip=source_ip,
            change_summary={"changed_fields": sorted(changed_fields)},
            created_at=now,
        )

    return AgentView.model_validate(agent)


async def set_agent_enabled(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    enabled: bool,
    session_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    source_ip: str | None = None,
) -> AgentView:
    """Enable or disable agent admission. Owner-scoped."""
    now = datetime.now(timezone.utc)
    stmt = (
        select(Agent)
        .where(Agent.id == agent_id, Agent.owner_user_id == owner_user_id)
        .with_for_update()
    )
    res = await db.execute(stmt)
    agent = res.scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")

    agent.enabled = enabled
    agent.updated_at = now
    await db.flush()

    action = "agent.enable" if enabled else "agent.disable"
    await record_audit(
        db,
        actor_kind="user",
        actor_id=owner_user_id,
        session_id=session_id,
        action=action,
        target_type="agent",
        target_id=agent.id,
        result="success",
        request_id=request_id,
        source_ip=source_ip,
        change_summary={"enabled": enabled},
        created_at=now,
    )

    return AgentView.model_validate(agent)


async def delete_agent(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    source_ip: str | None = None,
) -> None:
    """Delete agent and cascade drafts/versions. Owner-scoped."""
    now = datetime.now(timezone.utc)
    stmt = select(Agent).where(
        Agent.id == agent_id, Agent.owner_user_id == owner_user_id
    )
    res = await db.execute(stmt)
    agent = res.scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")

    await db.delete(agent)
    await db.flush()

    await record_audit(
        db,
        actor_kind="user",
        actor_id=owner_user_id,
        session_id=session_id,
        action="agent.delete",
        target_type="agent",
        target_id=agent_id,
        result="success",
        request_id=request_id,
        source_ip=source_ip,
        change_summary={"agent_id": str(agent_id)},
        created_at=now,
    )


# --- Draft Management & CAS ---------------------------------------------------


async def get_draft(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    catalog: ModelCatalog | None = None,
    capabilities: AdapterCapabilities | None = None,
    qualification_settings: AgentQualificationSettings | None = None,
) -> DraftView:
    """Get mutable draft with live executable validation summary. Owner-scoped."""
    await get_agent(db, agent_id=agent_id, owner_user_id=owner_user_id)

    stmt = select(AgentDraft).where(AgentDraft.agent_id == agent_id)
    res = await db.execute(stmt)
    draft = res.scalar_one_or_none()
    if draft is None:
        raise NotFoundError("Draft not found")

    summary = _validation_summary(
        draft.authoring_json, catalog, capabilities, qualification_settings
    )

    return DraftView(
        agent_id=draft.agent_id,
        draft_revision=draft.revision,
        authoring_hash=draft.authoring_hash,
        config=draft.authoring_json,
        validation=summary,
        updated_at=draft.updated_at,
    )


async def update_draft(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    payload: DraftUpdate,
    catalog: ModelCatalog | None = None,
    capabilities: AdapterCapabilities | None = None,
    qualification_settings: AgentQualificationSettings | None = None,
    session_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    source_ip: str | None = None,
) -> DraftView:
    """Compare-and-swap (CAS) update of agent draft.

    Enforces:
    - Expected revision must match current draft revision (409 REVISION_CONFLICT)
    - Forbidden secrets and structural authoring errors block save (422 VALIDATION)
    - Graph incompleteness CAN save; semantic errors are returned in DraftView.validation
    - Updates agent.draft_revision and draft.revision monotonically
    """
    now = datetime.now(timezone.utc)

    # 1. Lock agent and draft rows.
    agent_stmt = (
        select(Agent)
        .where(Agent.id == agent_id, Agent.owner_user_id == owner_user_id)
        .with_for_update()
    )
    agent_res = await db.execute(agent_stmt)
    agent = agent_res.scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")

    draft_stmt = (
        select(AgentDraft)
        .where(AgentDraft.agent_id == agent_id)
        .with_for_update()
    )
    draft_res = await db.execute(draft_stmt)
    draft = draft_res.scalar_one_or_none()
    if draft is None:
        raise NotFoundError("Draft not found")

    # 2. Strict CAS check.
    if draft.revision != payload.expected_revision:
        raise RevisionConflictError(
            f"Draft revision conflict: expected revision {payload.expected_revision}, "
            f"but current revision is {draft.revision}"
        )

    # 3. Credentials and structural validity block save.
    _raise_for_secret_keys(payload.config)
    structural_diagnostics = validate_draft_structure(payload.config)
    structural_errors = [d for d in structural_diagnostics if d.severity == "error"]
    if structural_errors:
        raise ValidationError(
            "Flow authoring structure is invalid",
            fields=[{"field": d.path, "code": d.code} for d in structural_errors],
        )

    # 4. Compute new hash and increment revision.
    new_authoring_hash = compute_authoring_hash(payload.config)
    next_revision = draft.revision + 1

    draft.authoring_json = payload.config
    draft.authoring_hash = new_authoring_hash
    draft.revision = next_revision
    draft.updated_at = now

    agent.draft_revision = next_revision
    agent.updated_at = now

    await db.flush()

    # 5. Record audit log.
    await record_audit(
        db,
        actor_kind="user",
        actor_id=owner_user_id,
        session_id=session_id,
        action="agent.draft_update",
        target_type="agent_draft",
        target_id=agent.id,
        result="success",
        request_id=request_id,
        source_ip=source_ip,
        change_summary={
            "agent_id": str(agent.id),
            "revision": next_revision,
            "authoring_hash": new_authoring_hash,
        },
        created_at=now,
    )

    # 6. Return updated view with live diagnostics.
    summary = _validation_summary(
        payload.config, catalog, capabilities, qualification_settings
    )

    return DraftView(
        agent_id=agent.id,
        draft_revision=next_revision,
        authoring_hash=new_authoring_hash,
        config=payload.config,
        validation=summary,
        updated_at=now,
    )


async def validate_draft(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    payload: ValidateRequest,
    capabilities: AdapterCapabilities | None = None,
    qualification_settings: AgentQualificationSettings | None = None,
    catalog: ModelCatalog | None = None,
) -> ValidationResponse:
    """Run full validation checks against saved draft or supplied configuration."""
    await get_agent(db, agent_id=agent_id, owner_user_id=owner_user_id)
    draft_stmt = select(AgentDraft).where(AgentDraft.agent_id == agent_id)
    draft_res = await db.execute(draft_stmt)
    draft = draft_res.scalar_one_or_none()
    if draft is None:
        raise NotFoundError("Draft not found")

    if (
        payload.expected_revision is not None
        and draft.revision != payload.expected_revision
    ):
        raise RevisionConflictError(
            f"Draft revision conflict: expected {payload.expected_revision}, "
            f"current is {draft.revision}"
        )

    if (
        payload.expected_authoring_hash is not None
        and draft.authoring_hash != payload.expected_authoring_hash
    ):
        raise AuthoringHashMismatchError(
            "Draft authoring hash mismatch: expected hash does not match current draft state"
        )

    config_to_check = (
        payload.config if payload.config is not None else draft.authoring_json
    )
    try:
        validate_config_bounds(config_to_check, FlowLimits().max_flow_bytes)
    except AgentConfigError as exc:
        raise _config_error(exc) from exc
    authoring_hash = compute_authoring_hash(config_to_check)
    secret_errors = secret_diagnostics(config_to_check)

    # 1. Structural check.
    structural_diagnostics = validate_draft_structure(config_to_check)
    structural_errors = [d for d in structural_diagnostics if d.severity == "error"]
    if structural_errors:
        err_views = diagnostics_to_views(secret_errors + structural_errors)
        return ValidationResponse(
            valid=False,
            eligible_for_publish=False,
            authoring_hash=authoring_hash,
            flow_hash=None,
            diagnostics=err_views,
            errors=err_views,
            warnings=[],
        )

    # 2. Catalog check.
    cat_diagnostics = validate_catalog_eligibility(config_to_check, catalog)

    sem_errors, warnings = validate_draft_semantics(
        config_to_check,
        capabilities=capabilities,
        qualification_settings=qualification_settings,
    )
    all_errors = secret_errors + cat_diagnostics + sem_errors
    is_valid = len(all_errors) == 0

    flow_hash = None
    if is_valid:
        try:
            flow = extract_flow(config_to_check)
            normalized = normalize_executable(flow)
            flow_hash = compute_flow_hash(normalized)
        except (AgentConfigError, Exception):
            flow_hash = None

    err_views = diagnostics_to_views(all_errors)
    warn_views = diagnostics_to_views(warnings)
    all_diags = err_views + warn_views

    return ValidationResponse(
        valid=is_valid,
        eligible_for_publish=is_valid,
        authoring_hash=authoring_hash,
        flow_hash=flow_hash,
        diagnostics=all_diags,
        errors=err_views,
        warnings=warn_views,
    )


# --- Versions & Publishing ----------------------------------------------------


async def publish_agent(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    payload: PublishRequest,
    limits: FlowLimits | None = None,
    catalog: ModelCatalog | None = None,
    capabilities: AdapterCapabilities | None = None,
    qualification_settings: AgentQualificationSettings | None = None,
    session_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    source_ip: str | None = None,
) -> VersionView:
    """Execute immutable version compilation and publishing transaction."""
    _, view = await publish_agent_version(
        db,
        agent_id=agent_id,
        owner_user_id=owner_user_id,
        expected_revision=payload.expected_revision,
        changelog=payload.changelog,
        acknowledged_warning_codes=payload.acknowledged_warning_codes,
        limits=limits,
        catalog=catalog,
        capabilities=capabilities,
        qualification_settings=qualification_settings,
        session_id=session_id,
        request_id=request_id,
        source_ip=source_ip,
    )
    return view


async def list_versions(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 20,
) -> VersionListResponse:
    """List published versions for an agent owned by user."""
    await get_agent(db, agent_id=agent_id, owner_user_id=owner_user_id)

    clamped_limit = max(1, min(limit, 100))
    stmt = (
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version.desc())
        .limit(clamped_limit + 1)
    )
    if cursor:
        try:
            cursor_ver = int(cursor)
            stmt = stmt.where(AgentVersion.version < cursor_ver)
        except ValueError:
            pass

    res = await db.execute(stmt)
    versions = list(res.scalars().all())

    next_cursor = None
    if len(versions) > clamped_limit:
        versions.pop()
        next_cursor = str(versions[-1].version)

    views = [VersionView.model_validate(v) for v in versions]
    return VersionListResponse(items=views, next_cursor=next_cursor)


async def get_version(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    version_ref: str,
) -> VersionDetailView:
    """Get single published version by version number or UUID. Owner-scoped."""
    await get_agent(db, agent_id=agent_id, owner_user_id=owner_user_id)

    stmt = select(AgentVersion).where(AgentVersion.agent_id == agent_id)
    if version_ref.isdigit():
        stmt = stmt.where(AgentVersion.version == int(version_ref))
    else:
        try:
            ver_uuid = uuid.UUID(version_ref)
            stmt = stmt.where(AgentVersion.id == ver_uuid)
        except ValueError:
            raise NotFoundError("Version not found")

    res = await db.execute(stmt)
    version = res.scalar_one_or_none()
    if version is None:
        raise NotFoundError("Version not found")

    return VersionDetailView.model_validate(version)


async def restore_draft(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    payload: RestoreDraftRequest,
    catalog: ModelCatalog | None = None,
    capabilities: AdapterCapabilities | None = None,
    qualification_settings: AgentQualificationSettings | None = None,
    session_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    source_ip: str | None = None,
) -> DraftView:
    """Restore mutable draft from a published version. Owner-scoped."""
    now = datetime.now(timezone.utc)

    # 1. Lock agent and draft.
    agent_stmt = (
        select(Agent)
        .where(Agent.id == agent_id, Agent.owner_user_id == owner_user_id)
        .with_for_update()
    )
    agent_res = await db.execute(agent_stmt)
    agent = agent_res.scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")

    draft_stmt = (
        select(AgentDraft)
        .where(AgentDraft.agent_id == agent_id)
        .with_for_update()
    )
    draft_res = await db.execute(draft_stmt)
    draft = draft_res.scalar_one_or_none()
    if draft is None:
        raise NotFoundError("Draft not found")

    # 2. CAS check.
    if draft.revision != payload.expected_revision:
        raise RevisionConflictError(
            f"Draft revision conflict: expected revision {payload.expected_revision}, "
            f"but current revision is {draft.revision}"
        )

    # 3. Lookup target published version.
    ver_stmt = select(AgentVersion).where(AgentVersion.agent_id == agent_id)
    if payload.version_id is not None:
        ver_stmt = ver_stmt.where(AgentVersion.id == payload.version_id)
    elif payload.version is not None:
        ver_stmt = ver_stmt.where(AgentVersion.version == payload.version)
    else:
        raise ValidationError("Must specify version_id or version number to restore")

    ver_res = await db.execute(ver_stmt)
    target_version = ver_res.scalar_one_or_none()
    if target_version is None:
        raise NotFoundError("Published version not found")

    # 4. Restore the original submitted config when available, otherwise the
    # immutable authoring flow. Never restore the normalized graph as source.
    immutable_cfg = target_version.immutable_config_json
    source_config = (
        immutable_cfg.get("authoring_config")
        or immutable_cfg.get("authoring_flow")
    )
    if not source_config:
        raise ValidationError(
            "Published version does not contain recoverable authoring configuration"
        )

    new_config = copy.deepcopy(source_config)
    try:
        validate_config_bounds(new_config, FlowLimits().max_flow_bytes)
    except AgentConfigError as exc:
        raise _config_error(exc) from exc
    new_authoring_hash = compute_authoring_hash(new_config)
    next_revision = draft.revision + 1

    draft.authoring_json = new_config
    draft.authoring_hash = new_authoring_hash
    draft.revision = next_revision
    draft.updated_at = now

    agent.draft_revision = next_revision
    agent.updated_at = now

    await db.flush()

    # 5. Record audit log.
    await record_audit(
        db,
        actor_kind="user",
        actor_id=owner_user_id,
        session_id=session_id,
        action="agent.restore_draft",
        target_type="agent_draft",
        target_id=agent.id,
        result="success",
        request_id=request_id,
        source_ip=source_ip,
        change_summary={
            "agent_id": str(agent.id),
            "restored_from_version": target_version.version,
            "new_revision": next_revision,
        },
        created_at=now,
    )

    summary = _validation_summary(
        new_config, catalog, capabilities, qualification_settings
    )

    return DraftView(
        agent_id=agent.id,
        draft_revision=next_revision,
        authoring_hash=new_authoring_hash,
        config=new_config,
        validation=summary,
        updated_at=now,
    )


async def duplicate_agent(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    payload: DuplicateAgentRequest,
    session_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    source_ip: str | None = None,
) -> AgentView:
    """Duplicate an existing agent and its draft/version flow for the owner."""
    now = datetime.now(timezone.utc)

    # 1. Verify source agent ownership.
    source_agent = await get_agent(
        db, agent_id=agent_id, owner_user_id=owner_user_id
    )

    # 2. Determine source config.
    config_to_copy: dict[str, Any] | None = None
    source_ref = payload.source_revision_or_version

    if source_ref is not None and str(source_ref).lower() not in ("draft", "current"):
        ref_str = str(source_ref)
        ver_stmt = select(AgentVersion).where(AgentVersion.agent_id == agent_id)
        if ref_str.isdigit():
            ver_stmt = ver_stmt.where(AgentVersion.version == int(ref_str))
        else:
            try:
                ver_stmt = ver_stmt.where(AgentVersion.id == uuid.UUID(ref_str))
            except ValueError:
                raise ValidationError(
                    "source_revision_or_version must be a version number, version UUID, or 'draft'",
                    fields=[
                        {
                            "field": "source_revision_or_version",
                            "code": "INVALID",
                        }
                    ],
                )
        ver_res = await db.execute(ver_stmt)
        ver = ver_res.scalar_one_or_none()
        if ver is None:
            raise NotFoundError("Source version to duplicate not found")
        immutable_cfg = ver.immutable_config_json
        config_to_copy = (
            immutable_cfg.get("authoring_config")
            or immutable_cfg.get("authoring_flow")
        )
        if not config_to_copy:
            raise ValidationError(
                "Source version does not contain recoverable authoring configuration"
            )

    if config_to_copy is None:
        draft_stmt = select(AgentDraft).where(AgentDraft.agent_id == agent_id)
        draft_res = await db.execute(draft_stmt)
        draft = draft_res.scalar_one_or_none()
        if draft is None:
            raise NotFoundError("Source draft not found")
        config_to_copy = draft.authoring_json

    new_config = copy.deepcopy(config_to_copy)
    try:
        validate_config_bounds(new_config, FlowLimits().max_flow_bytes)
    except AgentConfigError as exc:
        raise _config_error(exc) from exc
    new_hash = compute_authoring_hash(new_config)

    # 3. Create duplicate agent.
    new_agent = Agent(
        id=uuid.uuid4(),
        owner_user_id=owner_user_id,
        name=payload.name,
        description=payload.description or source_agent.description,
        persona_prompt=(
            payload.persona_prompt
            if payload.persona_prompt is not None
            else source_agent.persona_prompt
        ),
        enabled=False,
        draft_revision=1,
        current_published_version_id=None,
        created_at=now,
        updated_at=now,
    )
    db.add(new_agent)
    await db.flush()

    # 4. Create initial draft for new agent.
    new_draft = AgentDraft(
        agent_id=new_agent.id,
        authoring_json=new_config,
        authoring_hash=new_hash,
        revision=1,
        source="manual",
        template_version_id=None,
        updated_at=now,
    )
    db.add(new_draft)
    await db.flush()

    # 5. Record audit log.
    await record_audit(
        db,
        actor_kind="user",
        actor_id=owner_user_id,
        session_id=session_id,
        action="agent.duplicate",
        target_type="agent",
        target_id=new_agent.id,
        result="success",
        request_id=request_id,
        source_ip=source_ip,
        change_summary={
            "source_agent_id": str(agent_id),
            "new_agent_id": str(new_agent.id),
        },
        created_at=now,
    )

    return AgentView.model_validate(new_agent)


# --- Templates ----------------------------------------------------------------


async def ensure_default_templates(db: AsyncSession) -> None:
    """Seed platform-owned default templates if they are not present."""
    existing_res = await db.execute(
        select(AgentTemplateVersion.slug, AgentTemplateVersion.version).where(
            AgentTemplateVersion.version == 1
        )
    )
    existing = {(row[0], row[1]) for row in existing_res.all()}

    now = datetime.now(timezone.utc)
    missing = [
        {
            "id": uuid.uuid4(),
            "slug": template["slug"],
            "version": 1,
            "name": template["name"],
            "description": template["description"],
            "flow_json": copy.deepcopy(template["flow"]),
            "is_active": True,
            "created_at": now,
        }
        for template in DEFAULT_AGENT_TEMPLATES
        if (template["slug"], 1) not in existing
    ]
    if not missing:
        return


    await db.execute(
        pg_insert(AgentTemplateVersion)
        .values(missing)
        .on_conflict_do_nothing(index_elements=["slug", "version"])
    )
    await db.flush()


async def list_templates(
    db: AsyncSession,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> TemplateListResponse:
    """List available active agent templates."""
    await ensure_default_templates(db)

    clamped_limit = max(1, min(limit, 100))
    stmt = (
        select(AgentTemplateVersion)
        .where(AgentTemplateVersion.is_active == True)  # noqa: E712
        .order_by(AgentTemplateVersion.slug.asc(), AgentTemplateVersion.version.desc())
        .limit(clamped_limit + 1)
    )
    if cursor:
        stmt = stmt.where(AgentTemplateVersion.slug > cursor)

    res = await db.execute(stmt)
    templates = list(res.scalars().all())

    next_cursor = None
    if len(templates) > clamped_limit:
        templates.pop()
        next_cursor = templates[-1].slug

    views = [TemplateView.model_validate(t) for t in templates]
    return TemplateListResponse(items=views, next_cursor=next_cursor)


async def get_template(
    db: AsyncSession,
    *,
    template_id: uuid.UUID,
) -> TemplateDetailView:
    """Get detailed template definition by ID."""
    await ensure_default_templates(db)
    stmt = select(AgentTemplateVersion).where(
        AgentTemplateVersion.id == template_id,
        AgentTemplateVersion.is_active == True,  # noqa: E712
    )
    res = await db.execute(stmt)
    template = res.scalar_one_or_none()
    if template is None:
        raise NotFoundError("Agent template not found")

    return TemplateDetailView.model_validate(template)
