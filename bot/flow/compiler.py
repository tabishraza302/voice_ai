"""Flow compiler, normalization, manifest creation, and deterministic template rendering.

Per docs/first-release/flow-contract.md sections 4, 7:
- Produces immutable CompiledFlow with exact manifest fields and hashes.
- Never imports SQLAlchemy, bot.agents, or backend services.
- Deterministic template rendering and fragment generation.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from bot.flow.canonical import (
    canonical_json_v1,
    compute_artifact_hash,
    compute_flow_hash,
    compute_limits_hash,
)
from bot.flow.schemas import (
    AdapterCapabilities,
    CompiledFlow,
    FlowDiagnostic,
    FlowLimits,
    SessionState,
    SpeechFragment,
)
from bot.flow.tools import ToolRegistry
from bot.flow.validation import normalize_executable, validate_executable


def render_template(
    template: str,
    fields: dict[str, Any],
    locale: str = "en",
) -> str:
    """Render versioned authored message template against validated field state.

    Confirmed value -> string formatted.
    Inapplicable -> localized 'not applicable'.
    Unknown / Candidate -> localized 'not confirmed'.
    Refused -> localized 'not provided'.
    """
    loc_not_applicable = "not applicable" if locale == "en" else "लागू नहीं"
    loc_not_confirmed = "not confirmed" if locale == "en" else "पुष्टि नहीं हुई"
    loc_not_provided = "not provided" if locale == "en" else "प्रदान नहीं किया गया"

    def _replace(match: re.Match) -> str:
        fid = match.group(1).strip()
        f_state = fields.get(fid)
        if f_state is None:
            return loc_not_confirmed

        status = getattr(f_state, "status", None)
        applicability = getattr(f_state, "applicability", None)
        if status is None and isinstance(f_state, dict):
            status = f_state.get("status")
            applicability = f_state.get("applicability")

        if applicability == "inapplicable":
            return loc_not_applicable
        if status == "confirmed":
            val = getattr(f_state, "value", None)
            if val is None and isinstance(f_state, dict):
                val = f_state.get("value")
            return str(val) if val is not None else loc_not_confirmed
        if status == "refused":
            return loc_not_provided
        return loc_not_confirmed

    return re.sub(r"\{\{([^}]+)\}\}", _replace, template)


def split_text_into_fragments(text: str, max_chars: int = 1024) -> list[str]:
    """Deterministically split rendered message text into <=max_chars transport parts."""
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    current_len = 0

    # Split by sentences (English period/question/exclamation, Hindi danda ।)
    sentences = re.split(r"([.?!।]\s*)", text)
    paired = []
    i = 0
    while i < len(sentences):
        s = sentences[i]
        if i + 1 < len(sentences):
            s += sentences[i + 1]
            i += 2
        else:
            i += 1
        if s:
            paired.append(s)

    for sent in paired:
        if current_len + len(sent) <= max_chars:
            current.append(sent)
            current_len += len(sent)
        else:
            if current:
                parts.append("".join(current).strip())
                current = []
                current_len = 0
            if len(sent) <= max_chars:
                current.append(sent)
                current_len = len(sent)
            else:
                # Word-level split if sentence itself is overlarge
                words = sent.split(" ")
                w_curr = []
                w_len = 0
                for w in words:
                    if w_len + len(w) + 1 <= max_chars:
                        w_curr.append(w)
                        w_len += len(w) + 1
                    else:
                        if w_curr:
                            parts.append(" ".join(w_curr))
                        w_curr = [w]
                        w_len = len(w)
                if w_curr:
                    current = [" ".join(w_curr)]
                    current_len = len(current[0])

    if current:
        parts.append("".join(current).strip())

    return [p for p in parts if p]


def compute_dependency_fingerprint(
    template: str,
    fields: dict[str, Any],
) -> str:
    """Compute deterministic hash of the confirmed dependency values used in the template."""
    placeholders = re.findall(r"\{\{([^}]+)\}\}", template)
    deps: dict[str, Any] = {}
    for ph in sorted(set(p.strip() for p in placeholders)):
        f_state = fields.get(ph)
        if f_state:
            status = getattr(f_state, "status", None) or (f_state.get("status") if isinstance(f_state, dict) else None)
            val = getattr(f_state, "value", None) if hasattr(f_state, "value") else (f_state.get("value") if isinstance(f_state, dict) else None)
            deps[ph] = {"status": status, "value": val}
        else:
            deps[ph] = None
    return hashlib.sha256(canonical_json_v1(deps)).hexdigest()


def compile_flow(
    flow: dict[str, Any],
    policy: str = "",
    limits: FlowLimits | None = None,
    capabilities: AdapterCapabilities | None = None,
    tools: ToolRegistry | None = None,
    base_prompt_hash: str | None = None,
    revision_hash: str | None = None,
    tools_hash: str | None = None,
    knowledge_hash: str | None = None,
) -> CompiledFlow:
    """Compile authoring/executable flow into an immutable CompiledFlow artifact."""
    limits = limits or FlowLimits()
    tools = tools or ToolRegistry.empty()
    # 1. Normalize defaults
    normalized = normalize_executable(flow, limits)

    # 2. Validate executable
    diagnostics = validate_executable(normalized, limits, capabilities, tools=tools)
    errors = [d for d in diagnostics if d.severity == "error"]
    if errors:
        first_err = errors[0]
        raise ValueError(f"Flow compilation failed: {first_err.code} at {first_err.path} ({first_err.message})")

    # 3. Hashes
    flow_hash = compute_flow_hash(normalized)
    policy_hash = (
        hashlib.sha256(b"voice-ai/policy/v1\0" + policy.encode("utf-8")).hexdigest()
        if policy
        else ("0" * 64)
    )
    b_hash = base_prompt_hash or ("0" * 64)
    rev_hash = revision_hash or ("0" * 64)
    t_hash = tools_hash or tools.compute_tools_hash()
    k_hash = knowledge_hash or ("0" * 64)
    cap_ref = capabilities.report_hash if capabilities else ("0" * 64)
    limits_profile_hash = compute_limits_hash(limits.model_dump())

    # 4. Compact index
    steps = normalized.get("steps", [])
    step_ids = [s["id"] for s in steps]
    step_types = {s["id"]: s["type"] for s in steps}
    step_priorities = {s["id"]: s.get("priority", 100) for s in steps}

    compact_index = {
        "entry_step_id": normalized.get("entry_step_id", ""),
        "exit_step_id": normalized.get("exit_step_id", ""),
        "step_ids": step_ids,
        "step_types": step_types,
        "step_priorities": step_priorities,
    }

    # 5. Ordered step fragments and locale message templates
    ordered_step_fragments: dict[str, list[str]] = {}
    locale_message_templates: dict[str, list[dict[str, str]]] = {}
    for s in steps:
        sid = s["id"]
        msgs = s.get("messages", [])
        if msgs:
            ordered_step_fragments[sid] = [m["id"] for m in msgs]
            locale_message_templates[sid] = [
                {"id": m["id"], "en": m.get("en", ""), "hi": m.get("hi", "")}
                for m in msgs
            ]
        else:
            ordered_step_fragments[sid] = []
            locale_message_templates[sid] = []

    # 6. Evaluator rubric
    captures_summary = {}
    for s in steps:
        for c in s.get("captures", []):
            fid = c["field"]
            captures_summary[fid] = {
                "kind": c["kind"],
                "owner_step": s["id"],
                "required": c.get("required", True),
                "values": c.get("values", []),
            }

    evaluator_rubric = {
        "captures": captures_summary,
        "ordering": normalized.get("behavior", {}).get("ordering", "adaptive"),
    }

    manifest = {
        "adapter_qualification_references": cap_ref,
        "base_prompt_hash": b_hash,
        "canonicalization": "canonical_json_v1",
        "compact_index": compact_index,
        "compiler_version": "flow-compiler/1",
        "evaluator_rubric": evaluator_rubric,
        "flow_hash": flow_hash,
        "knowledge_references": [k_hash] if k_hash != "0" * 64 else [],
        "limits_profile_hash": limits_profile_hash,
        "locale_message_templates": locale_message_templates,
        "ordered_step_fragments": ordered_step_fragments,
        "playback_mode": capabilities.playback if capabilities else "unqualified",
        "policy_revision_hash": policy_hash,
        "schema_version": 1,
        "tool_references": [t_hash] if t_hash != "0" * 64 else [],
    }

    artifact_hash = compute_artifact_hash(manifest)

    return CompiledFlow(
        normalized=normalized,
        manifest=manifest,
        flow_hash=flow_hash,
        artifact_hash=artifact_hash,
        limits=limits,
    )
