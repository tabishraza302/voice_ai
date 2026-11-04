"""Read-only admin projections and audited versioned platform configuration writes."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import uuid
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.agents.defaults import DEFAULT_POLICY_SLUG, compute_policy_hash
from platform_api.agents.models import (
    Agent,
    AgentTemplateVersion,
    AgentVersion,
    PlatformPromptVersion,
)
from platform_api.agents.publishing import ensure_default_policy_version
from platform_api.agents.security import find_secret_paths
from platform_api.agents.service import ensure_default_templates
from platform_api.agents.validation import validate_draft_structure
from platform_api.audit import record_audit
from platform_api.auth.errors import ConflictError, NotFoundError, ValidationError
from platform_api.billing.models import CallBudget
from platform_api.billing.schemas import WalletEntriesPage, WalletView
from platform_api.billing.service import get_wallet_entries, get_wallet_view
from platform_api.models.auth import AuditLog, User
from platform_api.models.session import Session
from platform_api.models.wallet import WalletAccount
from platform_api.pagination import (
    PaginationError,
    decode_keyset_cursor,
    encode_keyset_cursor,
    normalize_date_range,
)
from platform_api.sessions.history import (
    AdminSessionRecord,
    SessionRecordsPage,
    billing_view_for_admin,
    get_admin_config_snapshot,
    get_admin_session_record,
    list_admin_session_records,
)
from platform_api.sessions.transcript_policy import (
    lock_transcript_policy,
    read_transcript_policy,
)
from platform_api.admin.schemas import (
    AdminAgentDetail,
    AdminAgentItem,
    AdminAgentPage,
    AdminAuditItem,
    AdminAuditPage,
    AdminOverview,
    AdminPromptItem,
    AdminPromptPage,
    AdminSessionDetail,
    AdminSessionItem,
    AdminSessionPage,
    AdminTemplateItem,
    AdminTemplatePage,
    PromptVersionRequest,
    AdminAgentDisableRequest,
    TemplateVersionRequest,
)
from bot.flow.schemas import FlowLimits


ADMIN_PAGE_MAX = 100
_SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= ADMIN_PAGE_MAX:
        raise ValidationError(f"limit must be between 1 and {ADMIN_PAGE_MAX}")


def _normalize_dates(
    from_at: datetime | None,
    to_at: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    try:
        return normalize_date_range(from_at, to_at)
    except PaginationError as exc:
        raise ValidationError(str(exc)) from exc


def _slug(value: str) -> str:
    if len(value) > 64 or _SLUG_PATTERN.fullmatch(value) is None:
        raise ValidationError(
            "slug must use lowercase letters, numbers, and single hyphens",
            fields=[{"field": "slug", "code": "INVALID"}],
        )
    return value


def _cursor_position(
    cursor: str | None,
    *,
    scope: str,
    filters: dict[str, Any],
):
    if cursor is None:
        return None
    try:
        return decode_keyset_cursor(cursor, scope=scope, filters=filters)
    except PaginationError as exc:
        raise ValidationError("Cursor is invalid for this query") from exc


def _next_cursor(
    rows: list[Any],
    *,
    limit: int,
    scope: str,
    filters: dict[str, Any],
) -> str | None:
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return encode_keyset_cursor(
        scope=scope,
        filters=filters,
        created_at=last.created_at,
        record_id=last.id,
    )


def _admin_session_item(record: AdminSessionRecord) -> AdminSessionItem:
    row = record.session
    return AdminSessionItem(
        session_id=row.id,
        owner_user_id=row.owner_user_id,
        agent_version_id=row.agent_version_id,
        kind=row.kind,
        state=row.state,
        session_version=row.version,
        runner_id=row.runner_id,
        connect_deadline=row.connect_deadline,
        hard_end_deadline=row.hard_end_deadline,
        created_at=row.created_at,
        ended_at=row.ended_at,
        failure_code=row.failure_code,
        billing=billing_view_for_admin(record.budget),
    )


async def get_admin_overview(
    db: AsyncSession,
    *,
    generated_at: datetime | None = None,
) -> AdminOverview:
    users_total = int((await db.scalar(select(func.count()).select_from(User))) or 0)
    users_active = int(
        (
            await db.scalar(
                select(func.count()).select_from(User).where(User.status == "active")
            )
        )
        or 0
    )

    agents_total = int((await db.scalar(select(func.count()).select_from(Agent))) or 0)
    owner_enabled_agents = int(
        (
            await db.scalar(
                select(func.count()).select_from(Agent).where(Agent.enabled.is_(True))
            )
        )
        or 0
    )
    sessions_total = int(
        (await db.scalar(select(func.count()).select_from(Session))) or 0
    )

    wallet_stats = (
        await db.execute(
            select(
                func.count(WalletAccount.user_id),
                func.coalesce(func.sum(WalletAccount.posted_minor), 0),
                func.coalesce(func.sum(WalletAccount.reserved_minor), 0),
                func.coalesce(
                    func.sum(WalletAccount.posted_minor - WalletAccount.reserved_minor),
                    0,
                ),
            )
        )
    ).one()
    budget_stats = (
        await db.execute(
            select(
                func.count(CallBudget.session_id)
                .filter(CallBudget.status == "OPEN"),
                func.count(CallBudget.session_id)
                .filter(CallBudget.status == "CLOSED"),
                func.coalesce(
                    func.sum(CallBudget.cumulative_charged_minor)
                    .filter(CallBudget.status == "OPEN"),
                    0,
                ),
                func.coalesce(
                    func.sum(CallBudget.cumulative_charged_minor)
                    .filter(CallBudget.status == "CLOSED"),
                    0,
                ),
            )
        )
    ).one()
    return AdminOverview(
        generated_at=generated_at or datetime.now(timezone.utc),
        users_total=users_total,
        users_active=users_active,
        agents_total=agents_total,
        owner_enabled_agents=owner_enabled_agents,
        sessions_total=sessions_total,
        wallet_accounts=int(wallet_stats[0] or 0),
        wallet_posted_minor=int(wallet_stats[1] or 0),
        wallet_reserved_minor=int(wallet_stats[2] or 0),
        wallet_available_minor=int(wallet_stats[3] or 0),
        call_budgets_open=int(budget_stats[0] or 0),
        call_budgets_closed=int(budget_stats[1] or 0),
        provisional_charged_minor=int(budget_stats[2] or 0),
        settled_charged_minor=int(budget_stats[3] or 0),
    )


async def list_admin_sessions(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID | None = None,
    state: str | None = None,
    kind: Literal["voice", "chat", "voice_test"] | None = None,
    agent_version_id: uuid.UUID | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> AdminSessionPage:
    page: SessionRecordsPage = await list_admin_session_records(
        db,
        owner_user_id=owner_user_id,
        state=state,
        kind=kind,
        agent_version_id=agent_version_id,
        from_at=from_at,
        to_at=to_at,
        limit=limit,
        cursor=cursor,
    )
    return AdminSessionPage(
        items=[_admin_session_item(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


async def get_admin_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
) -> AdminSessionDetail:
    record = await get_admin_session_record(db, session_id=session_id)
    item = _admin_session_item(record)
    if find_secret_paths(record.session.tariff_snapshot):
        raise ConflictError("Session tariff snapshot contains protected fields")
    config_snapshot = await get_admin_config_snapshot(db, session_id=session_id)
    await lock_transcript_policy(db, session_id, shared=True)
    transcript_policy = await read_transcript_policy(db, session_id)
    return AdminSessionDetail(
        session_id=item.session_id,
        owner_user_id=item.owner_user_id,
        agent_version_id=item.agent_version_id,
        kind=item.kind,
        state=item.state,
        session_version=item.session_version,
        runner_id=item.runner_id,
        connect_deadline=item.connect_deadline,
        hard_end_deadline=item.hard_end_deadline,
        created_at=item.created_at,
        ended_at=item.ended_at,
        failure_code=item.failure_code,
        billing=item.billing,
        tariff_snapshot=record.session.tariff_snapshot,
        pinned_config_snapshot=config_snapshot,
        transcript_visible_to_owner=transcript_policy.visible_to_owner,
        transcript_policy_revision=transcript_policy.revision,
    )


async def get_admin_wallet(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> WalletView:
    return await get_wallet_view(db, user_id=user_id)


async def list_admin_wallet_entries(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> WalletEntriesPage:
    return await get_wallet_entries(
        db,
        user_id=user_id,
        cursor=cursor,
        limit=limit,
    )


def _admin_agent_item(
    row: Agent,
    version: AgentVersion | None,
) -> AdminAgentItem:
    return AdminAgentItem(
        id=row.id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        description=row.description,
        enabled=row.enabled,
        draft_revision=row.draft_revision,
        current_published_version_id=row.current_published_version_id,
        current_published_version=version.version if version is not None else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )




async def disable_admin_agent(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    actor_id: uuid.UUID,
    auth_session_id: uuid.UUID,
    payload: AdminAgentDisableRequest,
    request_id: uuid.UUID,
    source_ip: str | None,
) -> AdminAgentItem:
    row = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Agent not found")
    was_enabled = row.enabled
    if was_enabled:
        row.enabled = False
        row.updated_at = datetime.now(timezone.utc)
        await db.flush()
    await record_audit(
        db,
        actor_kind="admin",
        actor_id=actor_id,
        session_id=auth_session_id,
        action="admin.agent.disable",
        target_type="agent",
        target_id=row.id,
        reason=payload.reason,
        request_id=request_id,
        source_ip=source_ip,
        result="success",
        change_summary={"enabled_before": was_enabled, "enabled_after": False},
    )
    version = (
        await db.get(AgentVersion, row.current_published_version_id)
        if row.current_published_version_id is not None
        else None
    )
    if row.current_published_version_id is not None and (
        version is None or version.agent_id != row.id
    ):
        raise ConflictError("Agent points to a missing published version")
    return _admin_agent_item(row, version)


async def list_admin_agents(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID | None = None,
    enabled: bool | None = None,
    q: str | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> AdminAgentPage:
    _validate_limit(limit)
    start, end = _normalize_dates(from_at, to_at)
    search = q.strip() if q is not None else None
    if search == "":
        search = None
    if search is not None and len(search) > 128:
        raise ValidationError("Agent search must not exceed 128 characters")
    filters = {
        "enabled": enabled,
        "from_at": start.isoformat() if start else None,
        "owner_user_id": str(owner_user_id) if owner_user_id else None,
        "q": search,
        "to_at": end.isoformat() if end else None,
    }
    position = _cursor_position(cursor, scope="admin.agents", filters=filters)
    stmt = select(Agent)
    if owner_user_id is not None:
        stmt = stmt.where(Agent.owner_user_id == owner_user_id)
    if enabled is not None:
        stmt = stmt.where(Agent.enabled.is_(enabled))
    if start is not None and end is not None:
        stmt = stmt.where(Agent.created_at >= start, Agent.created_at < end)
    if search is not None:
        search_value = search.lower()
        stmt = stmt.where(
            func.lower(Agent.name).contains(search_value, autoescape=True)
            | func.lower(Agent.description).contains(search_value, autoescape=True)
        )
    if position is not None:
        stmt = stmt.where(
            (Agent.created_at < position.created_at)
            | ((Agent.created_at == position.created_at) & (Agent.id < position.record_id))
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(Agent.created_at.desc(), Agent.id.desc()).limit(limit + 1)
            )
        ).scalars().all()
    )
    next_cursor = _next_cursor(
        rows,
        limit=limit,
        scope="admin.agents",
        filters=filters,
    )
    page_rows = rows[:limit]
    version_ids = tuple(
        row.current_published_version_id
        for row in page_rows
        if row.current_published_version_id is not None
    )
    versions: dict[uuid.UUID, AgentVersion] = {}
    if version_ids:
        versions = {
            version.id: version
            for version in (
                await db.execute(
                    select(AgentVersion).where(AgentVersion.id.in_(version_ids))
                )
            ).scalars().all()
        }
    items: list[AdminAgentItem] = []
    for row in page_rows:
        version = versions.get(row.current_published_version_id)
        if row.current_published_version_id is not None and (
            version is None or version.agent_id != row.id
        ):
            raise ConflictError("Agent points to a missing published version")
        items.append(_admin_agent_item(row, version))
    return AdminAgentPage(items=items, next_cursor=next_cursor)

async def get_admin_agent_detail(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
) -> AdminAgentDetail:
    row = await db.get(Agent, agent_id)
    if row is None:
        raise NotFoundError("Agent not found")
    owner = await db.get(User, row.owner_user_id)
    if owner is None:
        raise ConflictError("Agent owner is missing")
    version = (
        await db.get(AgentVersion, row.current_published_version_id)
        if row.current_published_version_id is not None
        else None
    )
    if row.current_published_version_id is not None and (
        version is None or version.agent_id != row.id
    ):
        raise ConflictError("Agent points to a missing published version")
    if version is not None and find_secret_paths(version.immutable_config_json):
        raise ConflictError("Published configuration contains protected fields")
    item = _admin_agent_item(row, version)
    return AdminAgentDetail(
        **item.model_dump(),
        owner_email=owner.email,
        owner_display_name=owner.display_name,
        immutable_config=version.immutable_config_json if version is not None else None,
        flow_hash=version.flow_hash if version is not None else None,
        artifact_hash=version.artifact_hash if version is not None else None,
        authoring_hash=version.authoring_hash if version is not None else None,
    )


async def list_admin_templates(
    db: AsyncSession,
    *,
    slug: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> AdminTemplatePage:
    await ensure_default_templates(db)
    _validate_limit(limit)
    if slug is not None:
        slug = _slug(slug)
    filters = {"slug": slug}
    position = _cursor_position(cursor, scope="admin.templates", filters=filters)
    stmt = select(AgentTemplateVersion)
    if slug is not None:
        stmt = stmt.where(AgentTemplateVersion.slug == slug)
    if position is not None:
        stmt = stmt.where(
            (AgentTemplateVersion.created_at < position.created_at)
            | (
                (AgentTemplateVersion.created_at == position.created_at)
                & (AgentTemplateVersion.id < position.record_id)
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(
                    AgentTemplateVersion.created_at.desc(), AgentTemplateVersion.id.desc()
                ).limit(limit + 1)
            )
        ).scalars().all()
    )
    next_cursor = _next_cursor(
        rows,
        limit=limit,
        scope="admin.templates",
        filters=filters,
    )
    return AdminTemplatePage(
        items=[_template_view(row) for row in rows[:limit]],
        next_cursor=next_cursor,
    )


async def list_admin_prompts(
    db: AsyncSession,
    *,
    slug: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> AdminPromptPage:
    await ensure_default_policy_version(db)
    _validate_limit(limit)
    if slug is not None:
        slug = _slug(slug)
    filters = {"slug": slug}
    position = _cursor_position(cursor, scope="admin.prompts", filters=filters)
    stmt = select(PlatformPromptVersion)
    if slug is not None:
        stmt = stmt.where(PlatformPromptVersion.slug == slug)
    if position is not None:
        stmt = stmt.where(
            (PlatformPromptVersion.created_at < position.created_at)
            | (
                (PlatformPromptVersion.created_at == position.created_at)
                & (PlatformPromptVersion.id < position.record_id)
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(
                    PlatformPromptVersion.created_at.desc(),
                    PlatformPromptVersion.id.desc(),
                ).limit(limit + 1)
            )
        ).scalars().all()
    )
    next_cursor = _next_cursor(
        rows,
        limit=limit,
        scope="admin.prompts",
        filters=filters,
    )
    return AdminPromptPage(
        items=[
            AdminPromptItem(
                id=row.id,
                slug=row.slug,
                version=row.version,
                prompt_text=row.prompt_text,
                policy_hash=row.policy_hash,
                is_active=row.is_active,
                created_at=row.created_at,
            )
            for row in rows[:limit]
        ],
        next_cursor=next_cursor,
    )


async def list_admin_audit_logs(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> AdminAuditPage:
    _validate_limit(limit)
    start, end = _normalize_dates(from_at, to_at)
    filters = {
        "action": action,
        "actor_id": str(actor_id) if actor_id else None,
        "from_at": start.isoformat() if start else None,
        "target_id": str(target_id) if target_id else None,
        "target_type": target_type,
        "to_at": end.isoformat() if end else None,
    }
    position = _cursor_position(cursor, scope="admin.audit", filters=filters)
    stmt = select(AuditLog)
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if target_type is not None:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if start is not None and end is not None:
        stmt = stmt.where(AuditLog.created_at >= start, AuditLog.created_at < end)
    if position is not None:
        stmt = stmt.where(
            (AuditLog.created_at < position.created_at)
            | ((AuditLog.created_at == position.created_at) & (AuditLog.id < position.record_id))
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit + 1)
            )
        ).scalars().all()
    )
    next_cursor = _next_cursor(
        rows,
        limit=limit,
        scope="admin.audit",
        filters=filters,
    )
    return AdminAuditPage(
        items=[
            AdminAuditItem(
                id=row.id,
                actor_kind=row.actor_kind,
                actor_id=row.actor_id,
                session_id=row.session_id,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                model_change=(
                    {
                        "provider_id": row.change_summary.get("provider_id"),
                        "model_id": row.change_summary.get("model_id"),
                        "before": {
                            key: row.change_summary["before"].get(key)
                            for key in ("enabled", "qualified")
                        } if isinstance(row.change_summary.get("before"), dict) else None,
                        "after": {
                            key: row.change_summary["after"].get(key)
                            for key in ("enabled", "qualified")
                        } if isinstance(row.change_summary.get("after"), dict) else None,
                    }
                    if row.target_type == "ollama_model"
                    and isinstance(row.change_summary, dict)
                    else None
                ),
                reason=row.reason,
                request_id=row.request_id,
                source_ip=str(row.source_ip) if row.source_ip is not None else None,
                result=row.result,
                created_at=row.created_at,
            )
            for row in rows[:limit]
        ],
        next_cursor=next_cursor,
    )


async def _lock_admin_resource(
    db: AsyncSession,
    *,
    resource: str,
    slug: str,
) -> None:
    lock_material = f"voice-ai/admin-resource-lock/v1\0{resource}\0{slug}".encode("utf-8")
    digest = hashlib.sha256(lock_material).digest()
    part_1 = int.from_bytes(digest[:4], "big", signed=True)
    part_2 = int.from_bytes(digest[4:8], "big", signed=True)
    await db.execute(select(func.pg_advisory_xact_lock(part_1, part_2)))


def _template_view(row: AgentTemplateVersion) -> AdminTemplateItem:
    if find_secret_paths(row.flow_json):
        raise ConflictError("Stored template contains protected fields")
    return AdminTemplateItem(
        id=row.id,
        slug=row.slug,
        version=row.version,
        name=row.name,
        description=row.description,
        flow_json=row.flow_json,
        is_active=row.is_active,
        created_at=row.created_at,
    )


def _prompt_view(row: PlatformPromptVersion) -> AdminPromptItem:
    return AdminPromptItem(
        id=row.id,
        slug=row.slug,
        version=row.version,
        prompt_text=row.prompt_text,
        policy_hash=row.policy_hash,
        is_active=row.is_active,
        created_at=row.created_at,
    )


async def create_template_version(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    auth_session_id: uuid.UUID,
    slug: str,
    payload: TemplateVersionRequest,
    request_id: uuid.UUID,
    source_ip: str | None,
) -> AdminTemplateItem:
    slug = _slug(slug)
    secret_paths = find_secret_paths(payload.flow_json)
    if secret_paths:
        raise ValidationError(
            "Template flow contains protected fields",
            fields=[{"field": path, "code": "SECRET_NOT_ALLOWED"} for path in secret_paths],
        )
    errors = [
        diagnostic
        for diagnostic in validate_draft_structure(payload.flow_json, FlowLimits())
        if diagnostic.severity == "error"
    ]
    if errors:
        raise ValidationError(
            "Template flow does not match the authoring contract",
            fields=[
                {"field": diagnostic.path or "flow_json", "code": diagnostic.code}
                for diagnostic in errors
            ],
        )

    await ensure_default_templates(db)
    await _lock_admin_resource(db, resource="template", slug=slug)
    active_rows = list(
        (
            await db.execute(
                select(AgentTemplateVersion)
                .where(
                    AgentTemplateVersion.slug == slug,
                    AgentTemplateVersion.is_active.is_(True),
                )
                .with_for_update()
            )
        ).scalars().all()
    )
    if len(active_rows) > 1:
        raise ConflictError("Template has more than one active version")
    latest_version = int(
        (
            await db.scalar(
                select(func.max(AgentTemplateVersion.version)).where(
                    AgentTemplateVersion.slug == slug
                )
            )
        )
        or 0
    )
    if active_rows:
        active_rows[0].is_active = False
    now = datetime.now(timezone.utc)
    row = AgentTemplateVersion(
        id=uuid.uuid4(),
        slug=slug,
        version=latest_version + 1,
        name=payload.name,
        description=payload.description,
        flow_json=payload.flow_json,
        is_active=True,
        created_at=now,
    )
    db.add(row)
    await db.flush()
    await record_audit(
        db,
        actor_kind="admin",
        actor_id=actor_id,
        session_id=auth_session_id,
        action="admin.template.version.create",
        target_type="agent_template_version",
        target_id=row.id,
        reason=payload.reason,
        request_id=request_id,
        source_ip=source_ip,
        result="success",
        change_summary={"slug": slug, "version": row.version, "active": True},
        created_at=now,
    )
    return _template_view(row)


async def get_template_version(
    db: AsyncSession,
    *,
    version_id: uuid.UUID,
) -> AdminTemplateItem:
    row = await db.get(AgentTemplateVersion, version_id)
    if row is None:
        raise NotFoundError("Template version not found")
    return _template_view(row)


async def create_prompt_version(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    auth_session_id: uuid.UUID,
    slug: str,
    payload: PromptVersionRequest,
    request_id: uuid.UUID,
    source_ip: str | None,
) -> AdminPromptItem:
    if slug != DEFAULT_POLICY_SLUG:
        raise NotFoundError("Platform prompt not found")
    await ensure_default_policy_version(db)
    await _lock_admin_resource(db, resource="prompt", slug=slug)
    active_rows = list(
        (
            await db.execute(
                select(PlatformPromptVersion)
                .where(
                    PlatformPromptVersion.slug == slug,
                    PlatformPromptVersion.is_active.is_(True),
                )
                .with_for_update()
            )
        ).scalars().all()
    )
    if len(active_rows) != 1:
        raise ConflictError("Platform prompt must have exactly one active version")
    latest_version = int(
        (
            await db.scalar(
                select(func.max(PlatformPromptVersion.version)).where(
                    PlatformPromptVersion.slug == slug
                )
            )
        )
        or 0
    )
    active_rows[0].is_active = False
    now = datetime.now(timezone.utc)
    row = PlatformPromptVersion(
        id=uuid.uuid4(),
        slug=slug,
        version=latest_version + 1,
        prompt_text=payload.prompt_text,
        policy_hash=compute_policy_hash(payload.prompt_text),
        is_active=True,
        created_at=now,
    )
    db.add(row)
    await db.flush()
    await record_audit(
        db,
        actor_kind="admin",
        actor_id=actor_id,
        session_id=auth_session_id,
        action="admin.prompt.version.create",
        target_type="platform_prompt_version",
        target_id=row.id,
        reason=payload.reason,
        request_id=request_id,
        source_ip=source_ip,
        result="success",
        change_summary={
            "slug": slug,
            "version": row.version,
            "policy_hash": row.policy_hash,
            "active": True,
        },
        created_at=now,
    )
    return _prompt_view(row)


async def get_prompt_version(
    db: AsyncSession,
    *,
    version_id: uuid.UUID,
) -> AdminPromptItem:
    row = await db.get(PlatformPromptVersion, version_id)
    if row is None:
        raise NotFoundError("Prompt version not found")
    return _prompt_view(row)


__all__ = [
    "create_prompt_version",
    "create_template_version",
    "get_admin_overview",
    "get_admin_session",
    "get_admin_wallet",
    "get_prompt_version",
    "get_template_version",
    "list_admin_agents",
    "list_admin_audit_logs",
    "list_admin_prompts",
    "list_admin_sessions",
    "list_admin_templates",
    "list_admin_wallet_entries",
    "disable_admin_agent",
]
