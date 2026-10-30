"""Immutable agent publishing and version compiler transaction.

Follows master spec III.5 and flow-contract.md:
- Atomic version transaction with pointer update
- Pinned policy, model, voice, flow, and artifact hashes
- Secrets rejected before compilation and scrubbed again before persistence
- Strict CAS revision verification (409 REVISION_CONFLICT)
- Stale authoring hash detection (409 AUTHORING_HASH_STALE)
- Warning code acknowledgment enforcement (422 VALIDATION)
- Full graph/policy/capability validation before publication
- Capabilities validated once and passed to the compiler so the manifest pins
  the exact adapter qualification report
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.flow.canonical import compute_authoring_hash
from bot.flow.compiler import compile_flow
from bot.flow.qualification import AgentQualificationSettings
from bot.flow.schemas import AdapterCapabilities, FlowLimits
from bot.flow.validation import normalize_executable
from bot.agents.compiler import compile_system_prompt
from platform_api.agents.config import (
    AgentConfigError,
    compute_model_hash,
    compute_settings_hash,
    compute_voice_hash,
    extract_flow,
    extract_settings,
    validate_config_bounds,
)
from platform_api.agents.defaults import (
    DEFAULT_BASE_PROMPT,
    DEFAULT_POLICY_SLUG,
    DEFAULT_POLICY_VERSION,
    compute_base_prompt_hash,
    compute_policy_hash,
)
from platform_api.agents.models import (
    Agent,
    AgentDraft,
    AgentVersion,
    PlatformPromptVersion,
)
from platform_api.agents.schemas import VersionView
from platform_api.agents.security import (
    find_secret_paths,
    scrub_secrets as _scrub_secrets,
)
from platform_api.agents.validation import (
    validate_catalog_eligibility,
    validate_draft_semantics,
    validate_draft_structure,
)
from platform_api.audit import record_audit
from platform_api.auth.errors import (
    AuthoringHashMismatchError,
    NotFoundError,
    RevisionConflictError,
    ValidationError,
)
from platform_api.catalog import ModelCatalog, load_catalog

__all__ = [
    "DEFAULT_BASE_PROMPT",
    "_scrub_secrets",
    "ensure_default_policy_version",
    "publish_agent_version",
]


async def ensure_default_policy_version(
    db: AsyncSession,
) -> PlatformPromptVersion:
    """Resolve the active platform policy, seeding the immutable default once."""
    stmt = (
        select(PlatformPromptVersion)
        .where(
            PlatformPromptVersion.slug == DEFAULT_POLICY_SLUG,
            PlatformPromptVersion.is_active == True,  # noqa: E712
        )
        .order_by(PlatformPromptVersion.version.desc())
        .limit(1)
    )
    res = await db.execute(stmt)
    prompt_version = res.scalar_one_or_none()
    if prompt_version is not None:
        return prompt_version

    values = {
        "id": uuid.uuid4(),
        "slug": DEFAULT_POLICY_SLUG,
        "version": DEFAULT_POLICY_VERSION,
        "prompt_text": DEFAULT_BASE_PROMPT,
        "policy_hash": compute_policy_hash(DEFAULT_BASE_PROMPT),
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
    }

    # Concurrent first publishes can race to seed the same immutable policy.
    # PostgreSQL's ON CONFLICT makes the loser observe the winner's row rather
    # than failing the publication with a unique violation.
    await db.execute(
        pg_insert(PlatformPromptVersion)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["slug", "version"])
    )
    await db.flush()
    res = await db.execute(stmt)
    prompt_version = res.scalar_one_or_none()
    if prompt_version is None:
        raise RuntimeError("Default platform policy could not be seeded")
    return prompt_version


def _validation_error_from_compiler(exc: ValueError) -> ValidationError:
    text = str(exc)
    code = "COMPILATION_FAILED"
    marker = "Flow compilation failed:"
    if marker in text:
        remainder = text.split(marker, 1)[1].strip()
        if remainder:
            code = remainder.split(" ", 1)[0]
    return ValidationError(
        "Flow could not be compiled for publication",
        fields=[{"field": "", "code": code}],
    )


async def publish_agent_version(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    expected_revision: int,
    changelog: str = "",
    acknowledged_warning_codes: list[str] | None = None,
    limits: FlowLimits | None = None,
    catalog: ModelCatalog | None = None,
    capabilities: AdapterCapabilities | None = None,
    qualification_settings: AgentQualificationSettings | None = None,
    session_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    source_ip: str | None = None,
) -> tuple[AgentVersion, VersionView]:
    """Compile and publish an immutable AgentVersion within the active transaction.

    Raises:
    - NotFoundError (404) if agent or draft does not exist or owner mismatch
    - RevisionConflictError (409) if expected_revision != draft.revision
    - AuthoringHashMismatchError (409) if the stored authoring hash is stale
    - ValidationError (422) if structural, semantic, catalog, capability, or
      warning checks fail
    """
    acknowledged_warning_codes = acknowledged_warning_codes or []
    acknowledged = set(acknowledged_warning_codes)
    cat = catalog if catalog is not None else load_catalog()
    lim = limits if limits is not None else FlowLimits()

    # 1. Lock agent and draft rows with FOR UPDATE.
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

    # 2. CAS revision check.
    if draft.revision != expected_revision:
        raise RevisionConflictError(
            f"Draft revision conflict: expected revision {expected_revision}, "
            f"but current revision is {draft.revision}"
        )

    # 3. Enforce envelope bounds before hashing the full submitted config.
    try:
        validate_config_bounds(draft.authoring_json, lim.max_flow_bytes)
    except AgentConfigError as exc:
        raise ValidationError(
            "Agent configuration exceeds authoring limits",
            fields=[{"field": exc.path, "code": "SCHEMA_INVALID"}],
        ) from exc

    # 4. Freshness / authoring hash check.
    current_authoring_hash = compute_authoring_hash(draft.authoring_json)
    if current_authoring_hash != draft.authoring_hash:
        raise AuthoringHashMismatchError(
            "Draft authoring hash mismatch: saved draft content is stale or corrupted"
        )

    # 5. Credentials never enter a draft or an immutable artifact.
    secret_paths = find_secret_paths(draft.authoring_json)
    if secret_paths:
        raise ValidationError(
            "Agent configuration contains forbidden secret fields",
            fields=[
                {"field": path, "code": "SECRET_NOT_ALLOWED"}
                for path in secret_paths
            ],
        )

    # 6. Structural validation.
    structural_diagnostics = validate_draft_structure(draft.authoring_json, lim)
    structural_errors = [d for d in structural_diagnostics if d.severity == "error"]
    if structural_errors:
        raise ValidationError(
            "Flow authoring structure is invalid and cannot be published",
            fields=[{"field": d.path, "code": d.code} for d in structural_errors],
        )

    # 7. Catalog qualification check.
    catalog_diagnostics = validate_catalog_eligibility(draft.authoring_json, cat)
    if catalog_diagnostics:
        raise ValidationError(
            "Draft contains unqualified model or voice configuration",
            fields=[{"field": d.path, "code": d.code} for d in catalog_diagnostics],
        )

    # 8. Semantic and capability checks. Capabilities are supplied by the
    # deployment profile, not by the customer payload, and are passed to the
    # compiler below so the manifest pins the exact qualification report.
    semantic_errors, warnings = validate_draft_semantics(
        draft.authoring_json,
        lim,
        capabilities,
        qualification_settings=qualification_settings,
    )

    if semantic_errors:
        raise ValidationError(
            "Flow has semantic or voice capability errors and cannot be published",
            fields=[{"field": d.path, "code": d.code} for d in semantic_errors],
        )
    unacknowledged = [
        warning.code for warning in warnings if warning.code not in acknowledged
    ]
    if unacknowledged:
        raise ValidationError(
            "Publication requires explicit acknowledgment of warning codes: "
            f"{sorted(set(unacknowledged))}",
            fields=[
                {
                    "field": "acknowledged_warning_codes",
                    "code": "UNACKNOWLEDGED_WARNINGS",
                }
            ],
        )

    # 9. Resolve the active platform policy revision.
    prompt_version = await ensure_default_policy_version(db)
    if compute_policy_hash(prompt_version.prompt_text) != prompt_version.policy_hash:
        raise ValidationError(
            "Active platform policy text does not match its pinned hash",
            fields=[{"field": "policy", "code": "HASH_MISMATCH"}],
        )

    # 10. Extract the flow and provider/voice settings from the draft envelope.
    try:
        flow = extract_flow(draft.authoring_json)
        settings = extract_settings(draft.authoring_json)
    except AgentConfigError as exc:
        raise ValidationError(
            "Agent configuration envelope is invalid",
            fields=[{"field": exc.path, "code": "SCHEMA_INVALID"}],
        ) from exc

    normalized = normalize_executable(flow, lim)

    # 11. Compile the immutable artifact. The compiler normalizes again, which
    # is idempotent for an already-normalized graph.
    compiled_base_prompt = compile_system_prompt(
        prompt_version.prompt_text,
        agent.persona_prompt,
    )
    base_prompt_hash = compute_base_prompt_hash(compiled_base_prompt)
    try:
        compiled = compile_flow(
            flow=normalized,
            policy=prompt_version.prompt_text,
            limits=lim,
            capabilities=capabilities,
            base_prompt_hash=base_prompt_hash,
        )
    except ValueError as exc:
        raise _validation_error_from_compiler(exc) from exc

    # 12. Compute the pinned model/voice/settings hashes.
    model_hash = compute_model_hash(settings)
    voice_hash = compute_voice_hash(settings)
    settings_hash = compute_settings_hash(settings)

    # 13. Construct the immutable configuration snapshot. The original source,
    # normalized graph and manifest are retained byte-for-byte; model/voice
    # selections are pinned both by ID and by deterministic hash.
    raw_config: dict[str, Any] = {
        "schema_version": 1,
        "compiler_version": "flow-compiler/1",
        "authoring_config": draft.authoring_json,
        "authoring_flow": flow,
        "normalized_flow": normalized,
        "manifest": compiled.manifest,
        "policy": {
            "version_id": str(prompt_version.id),
            "version": prompt_version.version,
            "slug": prompt_version.slug,
            "policy_hash": prompt_version.policy_hash,
            "prompt_text": prompt_version.prompt_text,
        },
        "persona_prompt": agent.persona_prompt,
        "model_pins": {
            **settings,
            "model_hash": model_hash,
            "voice_hash": voice_hash,
            "settings_hash": settings_hash,
        },
        "hashes": {
            "authoring_hash": draft.authoring_hash,
            "flow_hash": compiled.flow_hash,
            "artifact_hash": compiled.artifact_hash,
            "policy_hash": prompt_version.policy_hash,
            "base_prompt_hash": base_prompt_hash,
            "model_hash": model_hash,
            "voice_hash": voice_hash,
            "settings_hash": settings_hash,
        },
        "capabilities": (
            capabilities.model_dump(mode="json") if capabilities is not None else None
        ),
    }
    immutable_config = _scrub_secrets(raw_config)

    # Defence in depth: scrubbing must not alter the hashed source or manifest.
    if compute_authoring_hash(immutable_config["authoring_config"]) != draft.authoring_hash:
        raise ValidationError(
            "Immutable configuration scrubbing changed the authoring source",
            fields=[{"field": "", "code": "SECRET_NOT_ALLOWED"}],
        )
    if immutable_config["manifest"] != compiled.manifest:
        raise ValidationError(
            "Immutable configuration scrubbing changed the compiled manifest",
            fields=[{"field": "", "code": "SECRET_NOT_ALLOWED"}],
        )
    if (
        immutable_config["policy"]["prompt_text"] != prompt_version.prompt_text
        or compute_policy_hash(immutable_config["policy"]["prompt_text"])
        != prompt_version.policy_hash
    ):
        raise ValidationError(
            "Immutable policy text does not match its pinned hash",
            fields=[{"field": "policy", "code": "HASH_MISMATCH"}],
        )

    # 14. Determine the next append-only version number for this agent. The
    # agent row lock serializes concurrent publishes for the same agent.
    max_ver_stmt = select(func.coalesce(func.max(AgentVersion.version), 0)).where(
        AgentVersion.agent_id == agent.id
    )
    max_ver_res = await db.execute(max_ver_stmt)
    next_version = (max_ver_res.scalar() or 0) + 1

    # 15. Create the immutable AgentVersion record.
    now = datetime.now(timezone.utc)
    version = AgentVersion(
        id=uuid.uuid4(),
        agent_id=agent.id,
        version=next_version,
        immutable_config_json=immutable_config,
        authoring_hash=draft.authoring_hash,
        flow_hash=compiled.flow_hash,
        artifact_hash=compiled.artifact_hash,
        policy_version_id=prompt_version.id,
        compiler_version="flow-compiler/1",
        changelog=changelog,
        created_at=now,
    )
    db.add(version)
    await db.flush()

    # 16. Atomically move the agent's published pointer.
    agent.current_published_version_id = version.id
    agent.updated_at = now
    await db.flush()

    # 17. Record the append-only audit entry in the same transaction.
    await record_audit(
        db,
        actor_kind="user",
        actor_id=owner_user_id,
        session_id=session_id,
        action="agent.publish",
        target_type="agent_version",
        target_id=version.id,
        result="success",
        request_id=request_id,
        source_ip=source_ip,
        change_summary={
            "agent_id": str(agent.id),
            "version": version.version,
            "flow_hash": version.flow_hash,
            "artifact_hash": version.artifact_hash,
            "policy_hash": prompt_version.policy_hash,
            "model_hash": model_hash,
            "voice_hash": voice_hash,
        },
        created_at=now,
    )

    view = VersionView(
        version_id=version.id,
        agent_id=version.agent_id,
        version=version.version,
        flow_hash=version.flow_hash,
        artifact_hash=version.artifact_hash,
        authoring_hash=version.authoring_hash,
        compiler_version=version.compiler_version,
        changelog=version.changelog,
        created_at=version.created_at,
    )

    return (version, view)
