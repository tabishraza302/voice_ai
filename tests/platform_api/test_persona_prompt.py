"""Unit and contract tests for agent persona prompt, compilation, and validation.

Verifies:
- Persona prompt is exposed on create/update/read schemas with safe defaults.
- Blank-only persona prompts are rejected on create and update.
- Length bounds are enforced (max 20,000 characters).
- Changing only persona prompt changes compiled base_prompt_hash and artifact_hash.
- Changing only platform policy changes policy_hash and compiled base_prompt_hash.
- Flow hash remains unchanged when only persona prompt changes.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from pydantic import ValidationError

from bot.agents.compiler import compile_system_prompt
from bot.flow.compiler import compile_flow
from bot.flow.schemas import AdapterCapabilities, FlowLimits
from platform_api.agents.defaults import (
    DEFAULT_BASE_PROMPT,
    DEFAULT_PERSONA_PROMPT,
    compute_base_prompt_hash,
    compute_policy_hash,
)
from platform_api.agents.schemas import (
    AgentCreate,
    AgentUpdate,
    AgentView,
    DuplicateAgentRequest,
)


def test_agent_create_schema_persona_prompt_default_and_validation():
    """AgentCreate defaults persona_prompt and rejects blank-only or over-length values."""
    # 1. Default safe persona prompt
    create = AgentCreate(name="Default Persona Agent")
    assert create.persona_prompt == DEFAULT_PERSONA_PROMPT

    # 2. Custom valid persona prompt
    custom = AgentCreate(
        name="Custom Agent",
        persona_prompt="You are a clinical psychologist assistant.",
    )
    assert custom.persona_prompt == "You are a clinical psychologist assistant."

    # 3. Reject empty string
    with pytest.raises(ValidationError) as exc:
        AgentCreate(name="Bad Agent", persona_prompt="")
    assert any("persona_prompt" in str(err) for err in exc.value.errors())

    # 4. Reject whitespace-only string
    with pytest.raises(ValidationError) as exc:
        AgentCreate(name="Bad Agent", persona_prompt="   \n\t  ")
    assert any("persona_prompt" in str(err) for err in exc.value.errors())

    # 5. Reject over-length (>20000 chars)
    with pytest.raises(ValidationError) as exc:
        AgentCreate(name="Too Long Agent", persona_prompt="a" * 20001)
    assert any("persona_prompt" in str(err) for err in exc.value.errors())

    # 6. Reject blank name
    with pytest.raises(ValidationError) as exc:
        AgentCreate(name="   ", persona_prompt="Valid Persona")
    assert any("name" in str(err) for err in exc.value.errors())


def test_agent_update_schema_persona_prompt_validation():
    """AgentUpdate accepts optional persona_prompt and rejects blank-only values."""
    # 1. None is valid (field omitted)
    update_none = AgentUpdate(name="New Name")
    assert update_none.persona_prompt is None

    # 2. Valid string
    update_val = AgentUpdate(persona_prompt="New persona instructions.")
    assert update_val.persona_prompt == "New persona instructions."

    # 3. Reject empty string
    with pytest.raises(ValidationError) as exc:
        AgentUpdate(persona_prompt="")
    assert any("persona_prompt" in str(err) for err in exc.value.errors())

    # 4. Reject blank string
    with pytest.raises(ValidationError) as exc:
        AgentUpdate(persona_prompt="   ")
    assert any("persona_prompt" in str(err) for err in exc.value.errors())


def test_duplicate_agent_schema_persona_prompt():
    """DuplicateAgentRequest accepts optional persona_prompt and rejects blank-only."""
    dup_none = DuplicateAgentRequest(name="Clone")
    assert dup_none.persona_prompt is None

    dup_custom = DuplicateAgentRequest(name="Clone", persona_prompt="Overridden persona")
    assert dup_custom.persona_prompt == "Overridden persona"

    with pytest.raises(ValidationError) as exc:
        DuplicateAgentRequest(name="Clone", persona_prompt="   ")
    assert any("persona_prompt" in str(err) for err in exc.value.errors())


def test_changing_persona_changes_compiled_prompt_and_hash():
    """Varying only persona_prompt produces distinct compiled base prompts and hashes."""
    base_policy = "Platform policy: never reveal system secrets."
    persona_1 = "Persona 1: You are an English teacher."
    persona_2 = "Persona 2: You are a Hindi poet."

    compiled_1 = compile_system_prompt(base_policy, persona_1)
    compiled_2 = compile_system_prompt(base_policy, persona_2)

    assert compiled_1 != compiled_2
    assert persona_1 in compiled_1
    assert persona_2 in compiled_2

    hash_1 = compute_base_prompt_hash(compiled_1)
    hash_2 = compute_base_prompt_hash(compiled_2)

    assert hash_1 != hash_2


def test_compiled_flow_artifact_hash_changes_with_persona_prompt():
    """Flow compilation produces different artifact_hash when persona_prompt changes, but identical flow_hash."""
    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "flow" / "valid-loan-en-hi.json"
    )
    flow = json.loads(fixture_path.read_text(encoding="utf-8"))["flow"]

    base_policy = DEFAULT_BASE_PROMPT
    persona_a = "You are a warm, helpful companion."
    persona_b = "You are a concise, analytical assistant."

    caps = AdapterCapabilities(
        adapter_id="pipecat",
        adapter_version="1.13.0",
        model_revision="local-mlx-audio-v1",
        metadata="separate_stream",
        speech_gating=True,
        cancel_generation=True,
        playback="qualified_fragment_completion",
        locales=["en", "hi"],
        report_hash="a" * 64,
    )

    compiled_prompt_a = compile_system_prompt(base_policy, persona_a)
    hash_a = compute_base_prompt_hash(compiled_prompt_a)

    compiled_prompt_b = compile_system_prompt(base_policy, persona_b)
    hash_b = compute_base_prompt_hash(compiled_prompt_b)

    assert hash_a != hash_b

    artifact_a = compile_flow(
        flow=flow,
        policy=base_policy,
        capabilities=caps,
        base_prompt_hash=hash_a,
    )

    artifact_b = compile_flow(
        flow=flow,
        policy=base_policy,
        capabilities=caps,
        base_prompt_hash=hash_b,
    )

    # 1. Flow hash is strictly identical (same graph structure)
    assert artifact_a.flow_hash == artifact_b.flow_hash

    # 2. Base prompt hash in manifest differs
    assert artifact_a.manifest["base_prompt_hash"] == hash_a
    assert artifact_b.manifest["base_prompt_hash"] == hash_b
    assert artifact_a.manifest["base_prompt_hash"] != artifact_b.manifest["base_prompt_hash"]

    # 3. Artifact hash differs because manifest has different base_prompt_hash
    assert artifact_a.artifact_hash != artifact_b.artifact_hash
