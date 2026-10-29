"""Pure conversation flow controller, routing, capture invalidation, and event reducer.

Per docs/first-release/flow-contract.md sections 5, 6, 7, 8:
- Pure reducer with zero network, database, filesystem, or provider I/O.
- Deterministic first-match routing, acyclic DAG transitions.
- Automatic topological capture updates and dependency invalidation.
- Sealed fragment manifests, speech gating, barge-in / cancellation handling.
- Controller-rendered templates from validated state; LLM prose never authorizes transitions or money.
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any, Literal

from bot.flow.canonical import canonical_json_v1
from bot.flow.compiler import (
    compute_dependency_fingerprint,
    render_template,
    split_text_into_fragments,
)
from bot.flow.schemas import (
    Applicability,
    CancelGenerationCommand,
    CandidateFieldState,
    ChatCommittedOutputEvent,
    CompiledFlow,
    ConfirmedFieldState,
    EmitSpeechManifestCommand,
    Evidence,
    FieldState,
    FlowCommand,
    FlowDiagnostic,
    FlowEvent,
    FragmentPlayedOutputEvent,
    GenerationFailedModelEvent,
    Origin,
    Outcome,
    Reduction,
    RefusedFieldState,
    RequestActionCommand,
    RequestClarificationCommand,
    SessionState,
    SpeechFragment,
    StepStatus,
    TeardownCommand,
    UnknownFieldState,
)

STOP_PHRASES = {
    "stop",
    "stop now",
    "stop please",
    "quit",
    "exit",
    "cancel",
    "abort",
    "रुको",
    "बंद करो",
    "समाप्त करो",
    "रोकें",
}


def is_user_stop_text(text: str) -> bool:
    cleaned = text.strip().lower()
    return cleaned in STOP_PHRASES or any(cleaned.startswith(p) for p in ("stop", "exit", "quit", "cancel"))


CONTINUE_PHRASES = {
    "continue",
    "yes",
    "yes continue",
    "keep going",
    "proceed",
    "carry on",
    "हाँ",
    "हाँ जारी रखो",
    "जारी रखो",
    "आगे बढ़ो",
    "आगे",
}


def is_user_continue_text(text: str) -> bool:
    import re
    cleaned = re.sub(r"[^\w\s]", " ", text.strip().lower())
    words = cleaned.split()
    if not words:
        return False
    phrase = " ".join(words)
    if phrase in CONTINUE_PHRASES or any(w in CONTINUE_PHRASES for w in words):
        return True
    return any(p in phrase for p in ("continue", "proceed", "keep going", "जारी रखो", "आगे बढ़ो"))

FAQ_KEYWORDS = {
    "what", "how", "why", "when", "where", "who", "which",
    "fee", "fees", "charge", "charges", "cost", "annual fee", "rate", "interest",
    "क्या", "कितना", "कितनी", "कैसे", "कहाँ", "शुल्क", "फीस", "दर", "ब्याज"
}


def is_faq_question(text: str) -> bool:
    cleaned = text.strip().lower()
    if "?" in text or "؟" in text:
        return True
    import re
    words = set(re.findall(r"\w+", cleaned))
    return bool(words.intersection(FAQ_KEYWORDS))

AFFIRMATIVE_CONFIRMATION_PHRASES = {
    "yes", "yeah", "yep", "correct", "right", "that is correct", "thats right",
    "yes that is correct", "yes please", "sure", "confirmed", "true", "exactly",
    "हाँ", "हाँ सही है", "सही है", "बिल्कुल", "हाँजी", "जी हाँ"
}


def is_affirmative_confirmation(text: str) -> bool:
    import re
    cleaned = re.sub(r"[^\w\s]", " ", text.strip().lower())
    words = cleaned.split()
    if not words:
        return False
    phrase = " ".join(words)
    if phrase in AFFIRMATIVE_CONFIRMATION_PHRASES:
        return True
    if any(w in ("yes", "yeah", "yep", "correct", "right", "हाँ", "बिल्कुल", "हाँजी") for w in words):
        if not any(neg in words for neg in ("no", "not", "नहीं", "गलत", "don't", "dont")):
            return True
    return False


def validate_field_value(
    fid: str,
    val: Any,
    cap: dict[str, Any],
    u_idx: int,
    is_candidate: bool = False,
) -> list[FlowDiagnostic]:
    diags: list[FlowDiagnostic] = []
    kind = cap.get("kind")
    prefix = "Candidate value" if is_candidate else "Value"
    if kind == "enum":
        allowed_vals = cap.get("values", [])
        if not isinstance(val, str) or val not in allowed_vals:
            diags.append(
                FlowDiagnostic(
                    code="INVALID_PROPOSAL_VALUE",
                    path=f"/proposal/updates/{u_idx}/value",
                    message=f"{prefix} '{val}' is not in allowed enum values {allowed_vals}",
                )
            )
    elif kind == "boolean":
        if not isinstance(val, bool):
            diags.append(
                FlowDiagnostic(
                    code="INVALID_PROPOSAL_VALUE",
                    path=f"/proposal/updates/{u_idx}/value",
                    message=f"{prefix} for field '{fid}' expects boolean, got {type(val).__name__}",
                )
            )
    elif kind == "integer":
        if isinstance(val, bool) or not isinstance(val, int):
            diags.append(
                FlowDiagnostic(
                    code="INVALID_PROPOSAL_VALUE",
                    path=f"/proposal/updates/{u_idx}/value",
                    message=f"{prefix} for field '{fid}' expects integer, got {type(val).__name__}",
                )
            )
        else:
            min_v = cap.get("minimum", -9007199254740991)
            max_v = cap.get("maximum", 9007199254740991)
            if val < min_v or val > max_v:
                diags.append(
                    FlowDiagnostic(
                        code="INVALID_PROPOSAL_VALUE",
                        path=f"/proposal/updates/{u_idx}/value",
                        message=f"{prefix} {val} out of bounds [{min_v}, {max_v}] for field '{fid}'",
                    )
                )
    elif kind == "positive_integer":
        if isinstance(val, bool) or not isinstance(val, int) or val < 1:
            diags.append(
                FlowDiagnostic(
                    code="INVALID_PROPOSAL_VALUE",
                    path=f"/proposal/updates/{u_idx}/value",
                    message=f"{prefix} for field '{fid}' expects positive integer >= 1, got {val}",
                )
            )
        else:
            max_v = cap.get("maximum", 9007199254740991)
            if val > max_v:
                diags.append(
                    FlowDiagnostic(
                        code="INVALID_PROPOSAL_VALUE",
                        path=f"/proposal/updates/{u_idx}/value",
                        message=f"{prefix} {val} exceeds maximum {max_v} for field '{fid}'",
                    )
                )
    elif kind == "string":
        if not isinstance(val, str):
            diags.append(
                FlowDiagnostic(
                    code="INVALID_PROPOSAL_VALUE",
                    path=f"/proposal/updates/{u_idx}/value",
                    message=f"{prefix} for field '{fid}' expects string, got {type(val).__name__}",
                )
            )
        else:
            max_l = cap.get("max_length", 512)
            if len(val) > max_l:
                diags.append(
                    FlowDiagnostic(
                        code="INVALID_PROPOSAL_VALUE",
                        path=f"/proposal/updates/{u_idx}/value",
                        message=f"{prefix} length {len(val)} exceeds max_length {max_l} for field '{fid}'",
                    )
                )
    return diags


def initial_session_state(
    compiled: CompiledFlow,
    session_id: str,
    transport_epoch: str | None = None,
    locale: Literal["en", "hi"] = "en",
) -> SessionState:
    """Create fresh, pure SessionState for a session."""
    clean_session_id = session_id.strip() if session_id else ""
    if not clean_session_id:
        raise ValueError("initial_session_state requires a non-empty session_id")
    normalized = compiled.normalized
    entry_step_id = normalized.get("entry_step_id", "")
    # Initialize all declared fields across all steps
    fields: dict[str, FieldState] = {}
    steps_status: dict[str, StepStatus] = {}

    for step in normalized.get("steps", []):
        sid = step["id"]
        # Entry step is active, all other steps are pending
        steps_status[sid] = "active" if sid == entry_step_id else "pending"

        # Determine initial applicability based on step prerequisites
        prereqs = step.get("prerequisites", [])
        init_app: Applicability = "applicable" if not prereqs else "unknown"

        for cap in step.get("captures", []):
            fid = cap["field"]
            fields[fid] = UnknownFieldState(
                status="unknown",
                applicability=init_app,
                invalidated_by=None,
            )
    return SessionState(
        session_id=session_id,
        transport_epoch=transport_epoch,
        version=0,
        flow_hash=compiled.flow_hash,
        artifact_hash=compiled.artifact_hash,
        last_accepted_turn_id=None,
        generation_id=None,
        fields=fields,
        steps=steps_status,
        active_step_id=entry_step_id,
        resume_step_id=None,
        locale=locale,
        outcome="in_progress",
        teardown_emitted=False,
        clarification_attempts={},
        detour_counts={},
        offer_exit_pending={},
        continue_reset_used={},
        history_spoken_fragments=[],
        turn_history=[],
        committed_proposal_ids=[],
        active_manifest_id=None,
        active_manifest_fragments={},
        delivered_fragment_ids=[],
        delivered_semantic_fragment_ids=[],
        delivered_part_fingerprints={},
    )


def evaluate_predicate(
    cond: dict[str, Any],
    fields: dict[str, FieldState],
    steps_status: dict[str, StepStatus],
    compiled: CompiledFlow,
    source_step_id: str | None = None,
) -> Literal["TRUE", "FALSE", "UNKNOWN"]:
    """Evaluate a condition predicate to TRUE, FALSE, or UNKNOWN."""
    op = cond.get("operator")

    if op == "completed":
        if source_step_id:
            return "TRUE" if steps_status.get(source_step_id) == "complete" else "FALSE"
        return "FALSE"

    if op == "step_completed":
        ref_step = cond.get("step_id")
        return "TRUE" if steps_status.get(ref_step) == "complete" else "FALSE"

    if op == "all_applicable_required_fields_present":
        # Check all required captures across the flow
        steps = compiled.normalized.get("steps", [])
        for step in steps:
            captures = step.get("captures", [])
            if not captures:
                continue
            prereqs = step.get("prerequisites", [])
            step_prereq_val = "TRUE"
            for p in prereqs:
                p_res = evaluate_predicate(p, fields, steps_status, compiled, source_step_id=step["id"])
                if p_res == "FALSE":
                    step_prereq_val = "FALSE"
                    break
                elif p_res == "UNKNOWN":
                    step_prereq_val = "UNKNOWN"

            if step_prereq_val == "FALSE":
                # Step is inapplicable, its captures are inapplicable
                continue
            if step_prereq_val == "UNKNOWN":
                # Applicability is unknown, so required captures are unknown
                return "UNKNOWN"

            # Step is applicable; check each required capture
            for cap in step.get("captures", []):
                if not cap.get("required", True):
                    continue
                fid = cap["field"]
                f_state = fields.get(fid)
                if f_state is None:
                    return "UNKNOWN"
                if f_state.status == "refused":
                    return "FALSE"
                if f_state.status in ("unknown", "candidate"):
                    return "UNKNOWN"
                if f_state.status == "confirmed":
                    continue
        return "TRUE"

    # Field-based predicates
    fid = cond.get("field")
    if not fid or fid not in fields:
        return "UNKNOWN"

    f_state = fields[fid]
    status = f_state.status
    app = f_state.applicability

    # Inapplicable fields
    if app == "inapplicable":
        if op in ("is_unknown", "is_candidate", "is_refused", "is_present"):
            return "FALSE"
        return "UNKNOWN"

    # Explicit state inspectors
    if op == "is_unknown":
        return "TRUE" if status == "unknown" else "FALSE"
    if op == "is_candidate":
        return "TRUE" if status == "candidate" else "FALSE"
    if op == "is_refused":
        return "TRUE" if status == "refused" else "FALSE"
    if op == "is_present":
        if status == "confirmed":
            return "TRUE"
        if status == "refused":
            return "FALSE"
        return "UNKNOWN"

    # Value comparisons require confirmed, applicable typed value
    if status != "confirmed":
        if status == "refused":
            return "FALSE"
        return "UNKNOWN"

    # Confirmed value comparison
    val = getattr(f_state, "value", None)
    expected = cond.get("value")

    if op == "eq":
        return "TRUE" if val == expected else "FALSE"
    if op == "ne":
        return "TRUE" if val != expected else "FALSE"
    if op == "gt":
        return "TRUE" if val > expected else "FALSE"
    if op == "gte":
        return "TRUE" if val >= expected else "FALSE"
    if op == "lt":
        return "TRUE" if val < expected else "FALSE"
    if op == "lte":
        return "TRUE" if val <= expected else "FALSE"
    if op == "in":
        allowed = cond.get("values", [])
        return "TRUE" if val in allowed else "FALSE"

    return "UNKNOWN"


def evaluate_step_prerequisites(
    step: dict[str, Any],
    fields: dict[str, FieldState],
    steps_status: dict[str, StepStatus],
    compiled: CompiledFlow,
) -> Literal["TRUE", "FALSE", "UNKNOWN"]:
    """Evaluate all prerequisites of a step (AND conjunction)."""
    prereqs = step.get("prerequisites", [])
    if not prereqs:
        return "TRUE"

    has_unknown = False
    for p in prereqs:
        res = evaluate_predicate(p, fields, steps_status, compiled, source_step_id=step["id"])
        if res == "FALSE":
            return "FALSE"
        if res == "UNKNOWN":
            has_unknown = True

    return "UNKNOWN" if has_unknown else "TRUE"


def evaluate_transitions(
    step: dict[str, Any],
    fields: dict[str, FieldState],
    steps_status: dict[str, StepStatus],
    compiled: CompiledFlow,
) -> tuple[int | None, str | None, Literal["route", "clarify", "fallback"]]:
    """Evaluate transitions in stored order.

    Returns:
    (edge_idx, target_step_id, action)
    - First TRUE wins.
    - If no TRUE and any explicit UNKNOWN, clarify earliest unknown dependency (blocks ELSE).
    - If all explicit guards are FALSE, select ELSE if present, otherwise fallback.
    """
    transitions = step.get("transitions", [])
    has_unknown = False
    else_edge: tuple[int, str] | None = None

    for idx, t in enumerate(transitions):
        when = t.get("when", {})
        op = when.get("operator")

        if op == "else":
            else_edge = (idx, t["to"])
            continue

        res = evaluate_predicate(when, fields, steps_status, compiled, source_step_id=step["id"])
        if res == "TRUE":
            return (idx, t["to"], "route")
        if res == "UNKNOWN":
            has_unknown = True

    if has_unknown:
        # Earliest unknown dependency prevents ELSE
        return (None, None, "clarify")

    if else_edge is not None:
        return (else_edge[0], else_edge[1], "route")

    return (None, None, "fallback")


def resolve_target_step(
    target_id: str,
    fields: dict[str, FieldState],
    steps_status: dict[str, StepStatus],
    compiled: CompiledFlow,
) -> tuple[str | None, Literal["ok", "blocked"]]:
    """Resolve target step accounting for unmet prerequisites (adaptive priority, id)."""
    steps_by_id = {s["id"]: s for s in compiled.normalized.get("steps", [])}
    if target_id not in steps_by_id:
        return (None, "blocked")

    target_step = steps_by_id[target_id]
    prereq_val = evaluate_step_prerequisites(target_step, fields, steps_status, compiled)

    if prereq_val == "TRUE":
        return (target_id, "ok")

    # Target has unmet prerequisites; compute missing prerequisite owners
    # Transitive prerequisite step IDs
    missing_steps = []
    for sid, s in steps_by_id.items():
        if steps_status.get(sid) != "complete":
            # Check if target depends on this step directly or via field
            p_val = evaluate_step_prerequisites(s, fields, steps_status, compiled)
            if p_val != "FALSE":
                missing_steps.append(s)

    # Sort incomplete steps by priority ascending, then id
    missing_steps.sort(key=lambda s: (s.get("priority", 100), s["id"]))

    for candidate in missing_steps:
        # Check if candidate is eligible (its own prerequisites are TRUE)
        c_prereq = evaluate_step_prerequisites(candidate, fields, steps_status, compiled)
        if c_prereq == "TRUE":
            return (candidate["id"], "ok")

    return (None, "blocked")


def update_capture_applicability_and_invalidation(
    fields: dict[str, FieldState],
    steps_status: dict[str, StepStatus],
    compiled: CompiledFlow,
    changed_field: str | None = None,
) -> list[str]:
    """Recompute field applicability and cascade dependency invalidations.

    Returns list of invalidated step IDs.
    """
    invalidated_steps: list[str] = []
    steps_by_id = {s["id"]: s for s in compiled.normalized.get("steps", [])}

    for s in compiled.normalized.get("steps", []):
        sid = s["id"]
        prereq_val = evaluate_step_prerequisites(s, fields, steps_status, compiled)

        if prereq_val == "FALSE":
            # Step is inapplicable; invalidate its captures and completion
            for cap in s.get("captures", []):
                fid = cap["field"]
                curr_state = fields.get(fid)
                if curr_state and curr_state.applicability != "inapplicable":
                    fields[fid] = UnknownFieldState(
                        status="unknown",
                        applicability="inapplicable",
                        invalidated_by=changed_field or "dependency_change",
                    )
            if steps_status.get(sid) in ("complete", "active"):
                steps_status[sid] = "pending"
                if sid not in invalidated_steps:
                    invalidated_steps.append(sid)
        elif prereq_val == "UNKNOWN":
            if steps_status.get(sid) in ("complete", "active"):
                steps_status[sid] = "pending"
                if sid not in invalidated_steps:
                    invalidated_steps.append(sid)
        elif prereq_val == "TRUE":
            for cap in s.get("captures", []):
                fid = cap["field"]
                curr_state = fields.get(fid)
                if curr_state and curr_state.applicability == "inapplicable":
                    fields[fid] = UnknownFieldState(
                        status="unknown",
                        applicability="applicable",
                        invalidated_by=None,
                    )
                elif curr_state and curr_state.applicability == "unknown":
                    if curr_state.status == "unknown":
                        fields[fid] = UnknownFieldState(
                            status="unknown",
                            applicability="applicable",
                            invalidated_by=None,
                        )

    return invalidated_steps


def build_manifest_for_step(
    step_id: str,
    compiled: CompiledFlow,
    state: SessionState,
) -> EmitSpeechManifestCommand | None:
    """Build controller-owned speech manifest command for inform/summary/end steps."""
    steps_by_id = {s["id"]: s for s in compiled.normalized.get("steps", [])}
    step = steps_by_id.get(step_id)
    if not step:
        return None

    msgs = step.get("messages", [])
    if not msgs:
        return None

    manifest_id = f"manifest:{step_id}:{state.version}"
    fragments: list[SpeechFragment] = []
    if not state.transport_epoch or not state.transport_epoch.strip():
        return None
    epoch = state.transport_epoch
    turn_id = state.last_accepted_turn_id or "turn:0"
    gen_id = state.generation_id or "gen:0"

    for msg in msgs:
        mid = msg["id"]
        template = msg.get(state.locale, "")
        rendered = render_template(template, state.fields, state.locale)
        max_frag_chars = getattr(compiled, "limits", None) and compiled.limits.max_speech_fragment_chars or 1024
        parts = split_text_into_fragments(rendered, max_frag_chars)
        fingerprint = compute_dependency_fingerprint(template, state.fields)
        for part_idx, part_text in enumerate(parts):
            # Check if this exact message part was already delivered with the exact same dependency fingerprint
            delivered_fp = state.delivered_part_fingerprints.get(f"{step_id}:{mid}:{part_idx}")
            if delivered_fp is not None and delivered_fp == fingerprint:
                # Content and dependency fingerprint unchanged for this part: preserve across interruption and locale change
                continue

            content_hash = hashlib.sha256(part_text.encode("utf-8")).hexdigest()
            fragment_id = f"{manifest_id}:{step_id}:{mid}:{part_idx}"
            semantic_id = f"{step_id}:{mid}:{part_idx}:{content_hash}:{fingerprint}"
            fragments.append(
                SpeechFragment(
                    event_id=f"frag_evt:{fragment_id}",
                    session_id=state.session_id or manifest_id,
                    transport_epoch=epoch,
                    turn_id=turn_id,
                    generation_id=gen_id,
                    fragment_id=fragment_id,
                    semantic_fragment_id=semantic_id,
                    step_id=step_id,
                    state_version=state.version,
                    manifest_id=manifest_id,
                    content_hash=content_hash,
                    locale=state.locale,
                    text=part_text,
                    required=True,
                )
            )
    return EmitSpeechManifestCommand(
        manifest_id=manifest_id,
        step_id=step_id,
        fragments=fragments,
    )


def reduce_event(
    compiled: CompiledFlow,
    state: SessionState,
    event: FlowEvent,
) -> Reduction:
    """Pure reducer: (CompiledFlow, SessionState, FlowEvent) -> Reduction."""
    new_state = copy.deepcopy(state)
    commands: list[FlowCommand] = []
    diagnostics: list[FlowDiagnostic] = []
    event_type = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)

    # Ensure state is bound to a valid session
    if not state.session_id or not state.session_id.strip():
        diagnostics.append(
            FlowDiagnostic(
                code="UNBOUND_SESSION_STATE",
                path="/session_id",
                message="SessionState has invalid unbound session_id",
            )
        )
        return Reduction(state=new_state, commands=[], diagnostics=diagnostics)

    # Session identity binding across ALL events
    event_session_id = getattr(event, "session_id", None) or (event.get("session_id") if isinstance(event, dict) else None)
    if event_session_id != state.session_id:
        diagnostics.append(
            FlowDiagnostic(
                code="SESSION_MISMATCH",
                path="/session_id",
                message=f"Event session_id '{event_session_id}' does not match bound state session_id '{state.session_id}'",
            )
        )
        return Reduction(state=new_state, commands=[], diagnostics=diagnostics)
    # 1. User turn accepted event
    if event_type == "user_turn_accepted":
        turn_id = event.turn_id
        text = event.text
        gen_id = event.generation_id

        # Duplicate turn ID check across all recorded history
        existing_turn = next((t for t in state.turn_history if t.get("turn_id") == turn_id), None)
        if existing_turn is not None:
            if existing_turn.get("text") == text:
                # Idempotent re-delivery of an already recorded turn
                return Reduction(state=new_state, commands=[], diagnostics=[])
            # Conflicting text for duplicate turn_id fails with TURN_CONFLICT
            diagnostics.append(
                FlowDiagnostic(
                    code="TURN_CONFLICT",
                    path="/turn_history",
                    message=f"Duplicate turn_id '{turn_id}' with conflicting text",
                )
            )
            return Reduction(state=new_state, commands=[], diagnostics=diagnostics)
        # Ensure terminal state after teardown preempts all future events/teardowns (idempotent no-op)
        if state.teardown_emitted:
            return Reduction(state=new_state, commands=[], diagnostics=[])

        # Preempt prior generation if active
        if state.generation_id:
            commands.append(
                CancelGenerationCommand(
                    cancellation_id=f"cancel:{state.generation_id}",
                    reason="barge_in",
                )
            )

        # Record accepted user turn and advance state version as contract specifies
        new_state.version += 1
        new_state.last_accepted_turn_id = turn_id
        new_state.generation_id = gen_id
        if hasattr(event, "transport_epoch") and event.transport_epoch:
            new_state.transport_epoch = event.transport_epoch
        if hasattr(event, "locale") and event.locale:
            new_state.locale = event.locale
        new_state.turn_history.append(
            {
                "turn_id": turn_id,
                "generation_id": gen_id,
                "text": text,
                "state_version": new_state.version,
            }
        )

        # Check for immediate user stop (preempts closing)
        if is_user_stop_text(text):
            new_state.outcome = "aborted"
            new_state.teardown_emitted = True
            if not any(isinstance(c, CancelGenerationCommand) for c in commands):
                commands.append(
                    CancelGenerationCommand(
                        cancellation_id=f"cancel:{gen_id}",
                        reason="user_stop",
                    )
                )
            commands.append(
                TeardownCommand(
                    outcome="aborted",
                    reason="user_stop",
                )
            )
            return Reduction(state=new_state, commands=commands, diagnostics=[])

        # Check max_turns finite bound from compiled limits (budget termination preempts closing)
        max_turns = getattr(compiled, "limits", None) and compiled.limits.max_turns or 100
        if len(new_state.turn_history) >= max_turns:
            new_state.outcome = "partial"
            new_state.teardown_emitted = True
            exit_id = compiled.normalized.get("exit_step_id", "end")
            new_state.active_step_id = exit_id
            new_state.steps[exit_id] = "active"
            if not any(isinstance(c, CancelGenerationCommand) for c in commands):
                commands.append(
                    CancelGenerationCommand(
                        cancellation_id=f"cancel:{gen_id}",
                        reason="timeout",
                    )
                )
            commands.append(
                TeardownCommand(
                    outcome="partial",
                    reason="max_turns_exceeded",
                )
            )
            return Reduction(state=new_state, commands=commands, diagnostics=[])

        # Check offer_exit_pending on active step
        active_step_id = state.active_step_id
        steps_by_id = {s["id"]: s for s in compiled.normalized.get("steps", [])}
        if active_step_id and state.offer_exit_pending.get(active_step_id, False):
            if is_user_continue_text(text):
                if not state.continue_reset_used.get(active_step_id, False):
                    # Explicit continue resets attempts once for this objective
                    new_state.continue_reset_used[active_step_id] = True
                    new_state.offer_exit_pending[active_step_id] = False
                    new_state.clarification_attempts[active_step_id] = 0
                    commands.append(
                        RequestClarificationCommand(
                            field_id=steps_by_id.get(active_step_id, {}).get("captures", [{}])[0].get("field", ""),
                            step_id=active_step_id,
                            reason="continue_reset",
                        )
                    )
                    return Reduction(state=new_state, commands=commands, diagnostics=[])
                else:
                    # Second exhausted budget -> partial exit entering end step with closing manifest
                    new_state.outcome = "partial"
                    exit_id = compiled.normalized.get("exit_step_id", "end")
                    new_state.active_step_id = exit_id
                    new_state.steps[exit_id] = "active"
                    new_state.offer_exit_pending[active_step_id] = False
                    cmd = build_manifest_for_step(exit_id, compiled, new_state)
                    if cmd:
                        commands.append(cmd)
                        new_state.active_manifest_id = cmd.manifest_id
                        new_state.active_manifest_fragments = {
                            f.fragment_id: {
                                "content_hash": f.content_hash,
                                "required": f.required,
                                "step_id": f.step_id,
                                "semantic_fragment_id": f.semantic_fragment_id,
                            }
                            for f in cmd.fragments
                        }
                    return Reduction(state=new_state, commands=commands, diagnostics=[])
            else:
                # Unclear answer or declining the offer -> partial exit entering end step with closing manifest
                new_state.outcome = "partial"
                exit_id = compiled.normalized.get("exit_step_id", "end")
                new_state.active_step_id = exit_id
                new_state.steps[exit_id] = "active"
                new_state.offer_exit_pending[active_step_id] = False
                cmd = build_manifest_for_step(exit_id, compiled, new_state)
                if cmd:
                    commands.append(cmd)
                    new_state.active_manifest_id = cmd.manifest_id
                    new_state.active_manifest_fragments = {
                        f.fragment_id: {
                            "content_hash": f.content_hash,
                            "required": f.required,
                            "step_id": f.step_id,
                            "semantic_fragment_id": f.semantic_fragment_id,
                        }
                        for f in cmd.fragments
                    }
                return Reduction(state=new_state, commands=commands, diagnostics=[])

        return Reduction(state=new_state, commands=commands, diagnostics=[])

    # 2. Locale changed event
    elif event_type == "locale_changed":
        if state.teardown_emitted:
            return Reduction(state=new_state, commands=[], diagnostics=[])
        new_locale = event.locale
        if new_locale != new_state.locale:
            new_state.version += 1
            new_state.locale = new_locale

            # Build localized manifest for unplayed parts
            if new_state.active_step_id:
                steps_by_id = {s["id"]: s for s in compiled.normalized.get("steps", [])}
                curr_s = steps_by_id.get(new_state.active_step_id)
                if curr_s and curr_s.get("type") in ("inform", "summary", "end"):
                    cmd = build_manifest_for_step(new_state.active_step_id, compiled, new_state)
                    if cmd:
                        commands.append(cmd)
                        new_state.active_manifest_id = cmd.manifest_id
                        new_state.active_manifest_fragments = {
                            f.fragment_id: {
                                "content_hash": f.content_hash,
                                "required": f.required,
                                "step_id": f.step_id,
                                "semantic_fragment_id": f.semantic_fragment_id,
                            }
                            for f in cmd.fragments
                        }
        return Reduction(state=new_state, commands=commands, diagnostics=[])
    elif event_type in ("user_stopped", "session_terminated"):
        if state.teardown_emitted:
            # Idempotent: teardown already emitted
            return Reduction(state=new_state, commands=[], diagnostics=[])
        new_state.version += 1
        new_state.outcome = "aborted"
        new_state.teardown_emitted = True
        if state.generation_id:
            commands.append(
                CancelGenerationCommand(
                    cancellation_id=f"cancel:{state.generation_id}",
                    reason="user_stop",
                )
            )
        commands.append(
            TeardownCommand(
                outcome="aborted",
                reason=getattr(event, "reason", "user_stop"),
            )
        )
        return Reduction(state=new_state, commands=commands, diagnostics=[])

    # 4. State proposal event
    elif event_type == "state_proposal":
        if state.teardown_emitted:
            return Reduction(state=new_state, commands=[], diagnostics=[])
        proposal = event.proposal
        max_updates = getattr(compiled, "limits", None) and compiled.limits.max_field_updates_per_proposal or 32
        if len(proposal.updates) > max_updates:
            diagnostics.append(
                FlowDiagnostic(
                    code="INVALID_PROPOSAL",
                    path="/proposal/updates",
                    message=f"Proposal has {len(proposal.updates)} updates exceeding max_field_updates_per_proposal limit of {max_updates}",
                )
            )
            return Reduction(state=new_state, commands=[], diagnostics=diagnostics)
        if state.session_id and proposal.session_id != state.session_id:
            diagnostics.append(
                FlowDiagnostic(
                    code="SESSION_MISMATCH",
                    path="/proposal/session_id",
                    message=f"Proposal session_id '{proposal.session_id}' does not match bound state session_id '{state.session_id}'",
                )
            )
            return Reduction(state=new_state, commands=[], diagnostics=diagnostics)
        if hasattr(event, "session_id") and proposal.session_id != event.session_id:
            diagnostics.append(
                FlowDiagnostic(
                    code="SESSION_MISMATCH",
                    path="/proposal/session_id",
                    message=f"Proposal session_id '{proposal.session_id}' does not match event session_id '{event.session_id}'",
                )
            )
            return Reduction(state=new_state, commands=[], diagnostics=diagnostics)

        # 2. Generation match: must match current active generation
        if proposal.generation_id != state.generation_id:
            # Proposal from old or cancelled generation -> reject without state change
            return Reduction(state=new_state, commands=[], diagnostics=[])

        # 3. Turn binding: must match last accepted turn
        if proposal.turn_id != state.last_accepted_turn_id:
            diagnostics.append(
                FlowDiagnostic(
                    code="STALE_STATE",
                    path="/proposal/turn_id",
                    message=f"Proposal turn_id '{proposal.turn_id}' does not match active turn_id '{state.last_accepted_turn_id}'",
                )
            )
            return Reduction(state=new_state, commands=[], diagnostics=diagnostics)

        # 4. Base state version match
        if proposal.base_state_version != state.version:
            diagnostics.append(
                FlowDiagnostic(
                    code="STALE_STATE",
                    path="/proposal/base_state_version",
                    message=f"Proposal base version {proposal.base_state_version} does not match current state version {state.version}",
                )
            )
            return Reduction(state=new_state, commands=[], diagnostics=diagnostics)

        # 5. Proposal deduplication
        if proposal.proposal_id in state.committed_proposal_ids:
            # Idempotent re-delivery
            return Reduction(state=new_state, commands=[], diagnostics=[])

        # 6. Atomic validation over all updates
        steps_by_id = {s["id"]: s for s in compiled.normalized.get("steps", [])}
        all_captures: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for s in compiled.normalized.get("steps", []):
            for c in s.get("captures", []):
                all_captures[c["field"]] = (c, s)

        turns_by_id = {t["turn_id"]: t for t in state.turn_history}

        for u_idx, update in enumerate(proposal.updates):
            fid = update.field
            # Check field declared
            if fid not in all_captures:
                diagnostics.append(
                    FlowDiagnostic(
                        code="UNKNOWN_FIELD",
                        path=f"/proposal/updates/{u_idx}/field",
                        message=f"Proposed update targets undeclared field '{fid}'",
                    )
                )
                continue

            cap, owner_step = all_captures[fid]

            # Reject action/tool result capture targets from model proposal
            if owner_step.get("type") == "action":
                diagnostics.append(
                    FlowDiagnostic(
                        code="INVALID_PROPOSAL_TARGET",
                        path=f"/proposal/updates/{u_idx}/field",
                        message=f"Field '{fid}' is an action result capture and cannot be updated by model proposals",
                    )
                )
                continue

            # Evidence verification
            ev = getattr(update, "evidence", None)
            if not ev or not ev.source_turn_id or not ev.quote:
                diagnostics.append(
                    FlowDiagnostic(
                        code="INVALID_EVIDENCE",
                        path=f"/proposal/updates/{u_idx}/evidence",
                        message="Evidence source_turn_id and non-empty quote are required",
                    )
                )
                continue

            ev_turn = turns_by_id.get(ev.source_turn_id)
            if not ev_turn:
                diagnostics.append(
                    FlowDiagnostic(
                        code="INVALID_EVIDENCE",
                        path=f"/proposal/updates/{u_idx}/evidence/source_turn_id",
                        message=f"Evidence source_turn_id '{ev.source_turn_id}' does not exist in accepted turn history",
                    )
                )
                continue

            turn_text = ev_turn.get("text", "")
            if ev.quote not in turn_text:
                diagnostics.append(
                    FlowDiagnostic(
                        code="INVALID_EVIDENCE",
                        path=f"/proposal/updates/{u_idx}/evidence/quote",
                        message=f"Evidence quote '{ev.quote}' is not an exact substring of source turn text",
                    )
                )
                continue

            # Type, range, and enum validation
            if update.op == "propose":
                diagnostics.extend(validate_field_value(fid, update.value, cap, u_idx, is_candidate=False))
            elif update.op == "candidate":
                if update.value is not None:
                    diagnostics.extend(validate_field_value(fid, update.value, cap, u_idx, is_candidate=True))
            elif update.op == "confirm":
                curr_f = state.fields.get(fid)
                if not curr_f or curr_f.status != "candidate":
                    diagnostics.append(
                        FlowDiagnostic(
                            code="INVALID_CONFIRMATION",
                            path=f"/proposal/updates/{u_idx}/field",
                            message=f"Cannot confirm field '{fid}': field is not currently in candidate status",
                        )
                    )
                elif curr_f.value is None:
                    diagnostics.append(
                        FlowDiagnostic(
                            code="INVALID_CONFIRMATION",
                            path=f"/proposal/updates/{u_idx}/field",
                            message=f"Cannot confirm field '{fid}': candidate value is null",
                        )
                    )
                elif update.candidate_state_version != getattr(curr_f, "candidate_state_version", 0):
                    diagnostics.append(
                        FlowDiagnostic(
                            code="STALE_CONFIRMATION",
                            path=f"/proposal/updates/{u_idx}/candidate_state_version",
                            message=f"Stale confirmation: candidate version {update.candidate_state_version} does not match current candidate state version {getattr(curr_f, 'candidate_state_version', 0)}",
                        )
                    )
                if update.evidence.source_turn_id != state.last_accepted_turn_id:
                    diagnostics.append(
                        FlowDiagnostic(
                            code="INVALID_CONFIRMATION",
                            path=f"/proposal/updates/{u_idx}/evidence/source_turn_id",
                            message=f"Confirmation evidence source_turn_id '{update.evidence.source_turn_id}' does not match current accepted turn '{state.last_accepted_turn_id}'",
                        )
                    )
                elif not is_affirmative_confirmation(update.evidence.quote) or not is_affirmative_confirmation(turn_text):
                    diagnostics.append(
                        FlowDiagnostic(
                            code="INVALID_CONFIRMATION",
                            path=f"/proposal/updates/{u_idx}/evidence/quote",
                            message=f"Confirmation requires a clear affirmative answer from current turn, got '{update.evidence.quote}'",
                        )
                    )
            # Behavior check: allow_corrections
            allow_corr = compiled.normalized.get("behavior", {}).get("allow_corrections", True)
            if not allow_corr:
                curr_f = state.fields.get(fid)
                if curr_f and curr_f.status == "confirmed":
                    if update.op == "propose" and update.value != curr_f.value:
                        diagnostics.append(
                            FlowDiagnostic(
                                code="CORRECTION_DISALLOWED",
                                path=f"/proposal/updates/{u_idx}/field",
                                message=f"Correction disallowed: field '{fid}' is already confirmed and allow_corrections is false",
                            )
                        )
                    elif update.op in ("candidate", "refuse"):
                        diagnostics.append(
                            FlowDiagnostic(
                                code="CORRECTION_DISALLOWED",
                                path=f"/proposal/updates/{u_idx}/field",
                                message=f"Correction disallowed: field '{fid}' is already confirmed and allow_corrections is false",
                            )
                        )
        if diagnostics:
            curr_step = steps_by_id.get(state.active_step_id or "", {})
            first_field = curr_step.get("captures", [{}])[0].get("field", "")
            commands.append(
                RequestClarificationCommand(
                    field_id=first_field,
                    step_id=state.active_step_id or "",
                    reason="proposal_validation_failed",
                )
            )
            diagnostics.sort(key=lambda d: d.sort_key())
            return Reduction(state=new_state, commands=commands, diagnostics=diagnostics)

        # 7. Topological ordering and execution of updates
        # Build dependency ordering: fields whose owner steps have no prerequisites on other proposed fields go first
        proposed_fields = {u.field for u in proposal.updates}
        def _dep_weight(u):
            cap, step = all_captures[u.field]
            prereqs = step.get("prerequisites", [])
            # Count how many other proposed fields this step depends on
            deps = sum(1 for p in prereqs if p.get("field") in proposed_fields)
            return deps

        sorted_updates = sorted(proposal.updates, key=_dep_weight)
        changed_confirmed_fields: list[str] = []

        for update in sorted_updates:
            fid = update.field
            cap, owner_step = all_captures[fid]
            owner_prereq_val = evaluate_step_prerequisites(owner_step, new_state.fields, new_state.steps, compiled)

            if update.op == "propose":
                if owner_prereq_val == "FALSE":
                    new_state.fields[fid] = UnknownFieldState(
                        status="unknown",
                        applicability="inapplicable",
                    )
                elif owner_prereq_val == "UNKNOWN":
                    new_state.fields[fid] = CandidateFieldState(
                        status="candidate",
                        applicability="unknown",
                        value=update.value,
                        reason="dependency_unknown",
                        evidence=update.evidence,
                    )
                else:
                    # Check if confirmation:explicit is required
                    if cap.get("confirmation") == "explicit":
                        new_state.fields[fid] = CandidateFieldState(
                            status="candidate",
                            applicability="applicable",
                            value=update.value,
                            reason="needs_confirmation",
                            evidence=update.evidence,
                            candidate_state_version=state.version,
                        )
                    else:
                        new_state.fields[fid] = ConfirmedFieldState(
                            status="confirmed",
                            applicability="applicable",
                            value=update.value,
                            origin=Origin(
                                source_turn_id=proposal.turn_id,
                                generation_id=proposal.generation_id,
                                committed_state_version=new_state.version + 1,
                                source="user_capture",
                            ),
                        )
                        changed_confirmed_fields.append(fid)

            elif update.op == "candidate":
                new_state.fields[fid] = CandidateFieldState(
                    status="candidate",
                    applicability="applicable" if owner_prereq_val == "TRUE" else "unknown",
                    value=update.value,
                    reason=update.reason,
                    evidence=update.evidence,
                    candidate_state_version=state.version,
                )

            elif update.op == "refuse":
                new_state.fields[fid] = RefusedFieldState(
                    status="refused",
                    applicability="applicable" if owner_prereq_val == "TRUE" else "unknown",
                    origin=Origin(
                        source_turn_id=proposal.turn_id,
                        generation_id=proposal.generation_id,
                        committed_state_version=new_state.version + 1,
                        source="user_capture",
                    ),
                )

            elif update.op == "confirm":
                new_state.fields[fid] = ConfirmedFieldState(
                    status="confirmed",
                    applicability="applicable",
                    value=new_state.fields[fid].value,
                    origin=Origin(
                        source_turn_id=proposal.turn_id,
                        generation_id=proposal.generation_id,
                        committed_state_version=new_state.version + 1,
                        source="user_confirmation",
                    ),
                )
                changed_confirmed_fields.append(fid)

        # Record committed proposal ID and increment version together
        new_state.committed_proposal_ids.append(proposal.proposal_id)
        new_state.version += 1

        # Dependency invalidation on correction
        for cf in changed_confirmed_fields:
            update_capture_applicability_and_invalidation(
                new_state.fields,
                new_state.steps,
                compiled,
                changed_field=cf,
            )

        # Re-derive step completion status
        for s in compiled.normalized.get("steps", []):
            sid = s["id"]
            stype = s.get("type")
            if stype in ("ask", "collect"):
                caps = s.get("captures", [])
                req_caps = [c for c in caps if c.get("required", True)]
                if req_caps:
                    all_req_confirmed = all(
                        new_state.fields.get(c["field"])
                        and new_state.fields[c["field"]].status == "confirmed"
                        for c in req_caps
                    )
                    if all_req_confirmed:
                        new_state.steps[sid] = "complete"

        # Routing from owner of changed field or active step
        if changed_confirmed_fields:
            owner_id = next(
                (s["id"] for s in compiled.normalized.get("steps", []) if any(c["field"] in changed_confirmed_fields for c in s.get("captures", []))),
                None,
            )
            curr_step_id = owner_id or new_state.active_step_id
        else:
            curr_step_id = new_state.active_step_id
        visited_nodes: set[str] = set()
        while curr_step_id:
            if curr_step_id in visited_nodes:
                break
            visited_nodes.add(curr_step_id)
            curr_step = steps_by_id.get(curr_step_id)
            if not curr_step:
                break

            # If current step is complete, evaluate its transitions to advance
            if new_state.steps.get(curr_step_id) == "complete":
                edge_idx, target_id, action = evaluate_transitions(
                    curr_step, new_state.fields, new_state.steps, compiled
                )
                if action == "route" and target_id:
                    resolved_target, res_status = resolve_target_step(
                        target_id, new_state.fields, new_state.steps, compiled
                    )
                    if res_status == "ok" and resolved_target:
                        if resolved_target == curr_step_id:
                            break
                        # If resolved target is already complete, continue in memory
                        if new_state.steps.get(resolved_target) == "complete":
                            curr_step_id = resolved_target
                            continue

                        # Target is not complete or requires delivery
                        curr_step_id = resolved_target
                        new_state.active_step_id = resolved_target
                        new_state.steps[resolved_target] = "active"

                        t_type = steps_by_id[resolved_target].get("type")
                        if t_type in ("inform", "summary", "end"):
                            cmd = build_manifest_for_step(resolved_target, compiled, new_state)
                            if cmd:
                                commands.append(cmd)
                                new_state.active_manifest_id = cmd.manifest_id
                                new_state.active_manifest_fragments = {
                                    f.fragment_id: {
                                        "content_hash": f.content_hash,
                                        "required": f.required,
                                        "step_id": f.step_id,
                                        "semantic_fragment_id": f.semantic_fragment_id,
                                    }
                                    for f in cmd.fragments
                                }
                            if t_type == "end":
                                summary_step = next((s for s in compiled.normalized.get("steps", []) if s.get("type") == "summary"), None)
                                if summary_step and new_state.steps.get(summary_step["id"]) == "complete":
                                    new_state.outcome = "completed"
                                else:
                                    new_state.outcome = "partial"
                        break
                    elif res_status == "blocked":
                        new_state.outcome = "partial"
                        break
                elif action == "clarify":
                    commands.append(
                        RequestClarificationCommand(
                            field_id=curr_step.get("captures", [{}])[0].get("field", ""),
                            step_id=curr_step_id,
                            reason="unknown_dependency",
                        )
                    )
                    break
                else:
                    break
            else:
                # Active step is incomplete (needs user input or clarify)
                # Check if refusal edge exists
                edge_idx, target_id, action = evaluate_transitions(
                    curr_step, new_state.fields, new_state.steps, compiled
                )
                if action == "route" and target_id:
                    resolved_target, res_status = resolve_target_step(
                        target_id, new_state.fields, new_state.steps, compiled
                    )
                    if res_status == "ok" and resolved_target:
                        curr_step_id = resolved_target
                        new_state.active_step_id = resolved_target
                        new_state.steps[resolved_target] = "active"
                        t_type = steps_by_id[resolved_target].get("type")
                        if t_type == "end":
                            new_state.outcome = "partial"
                        if t_type in ("inform", "summary", "end"):
                            cmd = build_manifest_for_step(resolved_target, compiled, new_state)
                            if cmd:
                                commands.append(cmd)
                                new_state.active_manifest_id = cmd.manifest_id
                                new_state.active_manifest_fragments = {
                                    f.fragment_id: {
                                        "content_hash": f.content_hash,
                                        "required": f.required,
                                        "step_id": f.step_id,
                                        "semantic_fragment_id": f.semantic_fragment_id,
                                    }
                                    for f in cmd.fragments
                                }
                        break
                elif action == "clarify":
                    # Check whether the turn was an actual FAQ question vs an unsuccessful answer to the active objective
                    last_turn = new_state.turn_history[-1] if new_state.turn_history else {}
                    last_text = last_turn.get("text", "")
                    has_update = any(
                        any(c["field"] == u.field for c in curr_step.get("captures", []))
                        for u in proposal.updates
                    )
                    is_faq = is_faq_question(last_text) and not has_update

                    if is_faq:
                        # Relevant FAQ detour: save resume_step_id without incrementing clarification attempts
                        new_state.resume_step_id = curr_step_id
                        curr_detours = new_state.detour_counts.get(curr_step_id, 0)
                        max_detours = getattr(compiled, "limits", None) and compiled.limits.max_detours_per_objective or compiled.normalized.get("behavior", {}).get("max_detours_per_objective", 3)
                        if curr_detours < max_detours:
                            new_state.detour_counts[curr_step_id] = curr_detours + 1
                            commands.append(
                                RequestClarificationCommand(
                                    field_id=curr_step.get("captures", [{}])[0].get("field", ""),
                                    step_id=curr_step_id,
                                    reason="faq_resume",
                                )
                            )
                        else:
                            # Exhausted detours: redirect to current objective
                            commands.append(
                                RequestClarificationCommand(
                                    field_id=curr_step.get("captures", [{}])[0].get("field", ""),
                                    step_id=curr_step_id,
                                    reason="detour_limit_reached",
                                )
                            )
                        break
                    else:
                        # Unsuccessful answer to the active objective (candidate, unclear, or empty non-FAQ response)
                        attempts = new_state.clarification_attempts.get(curr_step_id, 0) + 1
                        new_state.clarification_attempts[curr_step_id] = attempts
                        fb = curr_step.get("fallback", {})
                        max_att = fb.get("max_attempts", compiled.normalized.get("behavior", {}).get("clarification_attempts", 2))
                        on_exhausted = fb.get("on_exhausted", "offer_exit")

                        if attempts >= max_att:
                            if on_exhausted == "offer_exit":
                                if not new_state.offer_exit_pending.get(curr_step_id, False):
                                    new_state.offer_exit_pending[curr_step_id] = True
                                    commands.append(
                                        RequestClarificationCommand(
                                            field_id=curr_step.get("captures", [{}])[0].get("field", ""),
                                            step_id=curr_step_id,
                                            reason="offer_exit",
                                        )
                                    )
                                    break
                                else:
                                    # Second exhausted budget -> exit partial
                                    new_state.outcome = "partial"
                                    exit_id = compiled.normalized.get("exit_step_id", "end")
                                    new_state.active_step_id = exit_id
                                    new_state.steps[exit_id] = "active"
                                    new_state.offer_exit_pending[curr_step_id] = False
                                    cmd = build_manifest_for_step(exit_id, compiled, new_state)
                                    if cmd:
                                        commands.append(cmd)
                                        new_state.active_manifest_id = cmd.manifest_id
                                        new_state.active_manifest_fragments = {
                                            f.fragment_id: {
                                                "content_hash": f.content_hash,
                                                "required": f.required,
                                                "step_id": f.step_id,
                                                "semantic_fragment_id": f.semantic_fragment_id,
                                            }
                                            for f in cmd.fragments
                                        }
                                    break
                            elif on_exhausted == "end_partial":
                                new_state.outcome = "partial"
                                exit_id = compiled.normalized.get("exit_step_id", "end")
                                new_state.active_step_id = exit_id
                                new_state.steps[exit_id] = "active"
                                cmd = build_manifest_for_step(exit_id, compiled, new_state)
                                if cmd:
                                    commands.append(cmd)
                                    new_state.active_manifest_id = cmd.manifest_id
                                    new_state.active_manifest_fragments = {
                                        f.fragment_id: {
                                            "content_hash": f.content_hash,
                                            "required": f.required,
                                            "step_id": f.step_id,
                                            "semantic_fragment_id": f.semantic_fragment_id,
                                        }
                                        for f in cmd.fragments
                                    }
                                break
                        else:
                            commands.append(
                                RequestClarificationCommand(
                                    field_id=curr_step.get("captures", [{}])[0].get("field", ""),
                                    step_id=curr_step_id,
                                    reason="clarification_needed",
                                )
                            )
                            break
        return Reduction(state=new_state, commands=commands, diagnostics=diagnostics)
    # 5. Failed generation events are terminal for the current model turn only.
    elif event_type == "generation_failed":
        if state.teardown_emitted:
            return Reduction(state=new_state, commands=[], diagnostics=[])
        if event.generation_id != state.generation_id:
            return Reduction(state=new_state, commands=[], diagnostics=[])
        if event.turn_id != state.last_accepted_turn_id:
            return Reduction(state=new_state, commands=[], diagnostics=[])
        evt_epoch = getattr(event, "transport_epoch", None)
        if state.transport_epoch and evt_epoch and evt_epoch != state.transport_epoch:
            return Reduction(state=new_state, commands=[], diagnostics=[])
        new_state.version += 1
        new_state.outcome = "failed"
        new_state.teardown_emitted = True
        new_state.generation_id = None
        commands.append(
            TeardownCommand(outcome="failed", reason="generation_failed")
        )
        return Reduction(state=new_state, commands=commands, diagnostics=[])

    # 6. Playback or Chat delivery events
    elif event_type in ("fragment_played", "chat_committed"):
        if state.teardown_emitted:
            return Reduction(state=new_state, commands=[], diagnostics=[])
        gen_id = event.generation_id
        if gen_id != state.generation_id:
            # Stale or old generation delivery -> ignore without state changes
            return Reduction(state=new_state, commands=[], diagnostics=[])

        # Transport epoch check
        evt_epoch = getattr(event, "transport_epoch", None)
        if state.transport_epoch and evt_epoch and evt_epoch != state.transport_epoch:
            # Stale or wrong transport epoch -> ignore without state changes
            return Reduction(state=new_state, commands=[], diagnostics=[])

        # Active step must be an output step (inform, summary, end)
        active_step_id = new_state.active_step_id
        steps_by_id = {s["id"]: s for s in compiled.normalized.get("steps", [])}
        active_step = steps_by_id.get(active_step_id) if active_step_id else None

        if not active_step or active_step.get("type") not in ("inform", "summary", "end"):
            return Reduction(state=new_state, commands=[], diagnostics=[])

        # Strict manifest binding: must have an active sealed manifest and fragment record
        if not state.active_manifest_id or not state.active_manifest_fragments:
            # Absent active manifest: reject invented delivery without a sealed manifest
            return Reduction(state=new_state, commands=[], diagnostics=[])

        if event.manifest_id != state.active_manifest_id:
            # Mismatched manifest ID
            return Reduction(state=new_state, commands=[], diagnostics=[])

        frag_meta = state.active_manifest_fragments.get(event.fragment_id)
        if not frag_meta:
            # Unknown fragment ID not in active manifest
            return Reduction(state=new_state, commands=[], diagnostics=[])

        if event_type == "fragment_played":
            # 1. Flow-level qualification gate: fail closed if compiled without qualified voice playback
            if compiled.manifest.get("playback_mode") != "qualified_fragment_completion":
                # Voice playback is unqualified for this compiled flow
                return Reduction(state=new_state, commands=[], diagnostics=[])

            # 2. Qualified capability report reference verification against compiled manifest
            trusted_report_hash = compiled.manifest.get("adapter_qualification_references")
            if not trusted_report_hash or trusted_report_hash == "0" * 64:
                return Reduction(state=new_state, commands=[], diagnostics=[])
            if getattr(event, "capability_report_hash", None) != trusted_report_hash:
                # Forged or untrusted capability report hash
                return Reduction(state=new_state, commands=[], diagnostics=[])

            # 3. Exact content hash verification against sealed manifest fragment
            if getattr(event, "content_hash", None) != frag_meta.get("content_hash"):
                # Forged or mismatched content hash
                return Reduction(state=new_state, commands=[], diagnostics=[])

        elif event_type == "chat_committed":
            # Durable delivery ID binding: must be non-empty string
            durable_id = getattr(event, "durable_delivery_id", None)
            if not durable_id or not str(durable_id).strip():
                # Preview token or unfinished write cannot complete message
                return Reduction(state=new_state, commands=[], diagnostics=[])

        frag_id = event.fragment_id
        if frag_id not in new_state.delivered_fragment_ids:
            new_state.delivered_fragment_ids.append(frag_id)
        if frag_id not in new_state.history_spoken_fragments:
            new_state.history_spoken_fragments.append(frag_id)

        sem_id = frag_meta.get("semantic_fragment_id", "")
        if sem_id:
            if sem_id not in new_state.delivered_semantic_fragment_ids:
                new_state.delivered_semantic_fragment_ids.append(sem_id)
            parts = sem_id.split(":")
            if len(parts) >= 5:
                s_id, m_id, p_idx, fp = parts[0], parts[1], parts[2], parts[4]
                new_state.delivered_part_fingerprints[f"{s_id}:{m_id}:{p_idx}"] = fp
        # Completion check: all required fragments for this active manifest must be delivered
        req_frags = [fid for fid, m in state.active_manifest_fragments.items() if m.get("required", True)]
        is_complete = all(fid in new_state.delivered_fragment_ids for fid in req_frags)

        if is_complete:
            new_state.steps[active_step_id] = "complete"
            new_state.active_manifest_id = None
            new_state.active_manifest_fragments = {}

            if active_step.get("type") == "end":
                new_state.teardown_emitted = True
                commands.append(
                    TeardownCommand(
                        outcome=new_state.outcome if new_state.outcome != "in_progress" else "completed",
                        reason="end_delivery_complete",
                    )
                )
            else:
                # Transition from complete inform/summary step
                edge_idx, target_id, action = evaluate_transitions(
                    active_step, new_state.fields, new_state.steps, compiled
                )
                if action == "route" and target_id:
                    resolved_target, res_status = resolve_target_step(
                        target_id, new_state.fields, new_state.steps, compiled
                    )
                    if res_status == "ok" and resolved_target:
                        new_state.active_step_id = resolved_target
                        new_state.steps[resolved_target] = "active"
                        t_type = steps_by_id[resolved_target].get("type")
                        if t_type in ("inform", "summary", "end"):
                            cmd = build_manifest_for_step(resolved_target, compiled, new_state)
                            if cmd:
                                commands.append(cmd)
                                new_state.active_manifest_id = cmd.manifest_id
                                new_state.active_manifest_fragments = {
                                    f.fragment_id: {
                                        "content_hash": f.content_hash,
                                        "required": f.required,
                                        "step_id": f.step_id,
                                        "semantic_fragment_id": f.semantic_fragment_id,
                                    }
                                    for f in cmd.fragments
                                }
                        if t_type == "end":
                            summary_step = next(
                                (s for s in compiled.normalized.get("steps", []) if s.get("type") == "summary"),
                                None,
                            )
                            if summary_step and new_state.steps.get(summary_step["id"]) == "complete":
                                new_state.outcome = "completed"
                            else:
                                new_state.outcome = "partial"

        new_state.version += 1
        return Reduction(state=new_state, commands=commands, diagnostics=[])

    # 6. Action completed event
    elif event_type == "action_completed":
        if state.teardown_emitted:
            return Reduction(state=new_state, commands=[], diagnostics=[])
        steps_by_id = {s["id"]: s for s in compiled.normalized.get("steps", [])}
        active_step_id = state.active_step_id
        active_step = steps_by_id.get(active_step_id or "")

        # Check 1: Active step must be an action step
        if not active_step or active_step.get("type") != "action":
            step_type = active_step.get("type") if active_step else "none"
            diagnostics.append(
                FlowDiagnostic(
                    code="FORGED_ACTION",
                    path="/active_step_id",
                    message=f"action_completed rejected: active step '{active_step_id}' is of type '{step_type}', not 'action'",
                )
            )
            return Reduction(state=copy.deepcopy(state), commands=[], diagnostics=diagnostics)

        # Check 2: Absent previously controller-authorized request and validated result
        diagnostics.append(
            FlowDiagnostic(
                code="UNAUTHORIZED_ACTION",
                path="/active_step_id",
                message=f"action_completed rejected: no controller-authorized RequestAction exists for step '{active_step_id}'",
            )
        )
        return Reduction(state=copy.deepcopy(state), commands=[], diagnostics=diagnostics)

    # Default / unhandled
    return Reduction(state=new_state, commands=commands, diagnostics=[])
