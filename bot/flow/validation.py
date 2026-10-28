"""Validation and normalization for conversation flows.

Per docs/first-release/flow-contract.md sections 2, 3:
- validate_authoring: JSON Schema 2020-12 authoring checks and size bounds.
- normalize_executable: fill documented defaults, merge behavior, bounds.
- validate_executable: executable schema checks, semantic diagnostics, mode capability checks.
Diagnostics are sorted by (path, code, message).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema

from bot.flow.schemas import (
    AdapterCapabilities,
    FlowDiagnostic,
    FlowLimits,
    MAX_SAFE_INTEGER,
    MIN_SAFE_INTEGER,
)
from bot.flow.tools import ToolRegistry

_SCHEMAS_DIR = Path(__file__).resolve().parent

with open(_SCHEMAS_DIR / "flow-authoring.schema.json", "r", encoding="utf-8") as _f:
    _AUTHORING_SCHEMA = json.load(_f)

with open(_SCHEMAS_DIR / "flow.schema.json", "r", encoding="utf-8") as _f:
    _EXECUTABLE_SCHEMA = json.load(_f)

_authoring_validator = jsonschema.Draft202012Validator(_AUTHORING_SCHEMA)
_executable_validator = jsonschema.Draft202012Validator(_EXECUTABLE_SCHEMA)


def validate_authoring(
    source: dict[str, Any],
    limits: FlowLimits | None = None,
) -> list[FlowDiagnostic]:
    """Validate flow authoring structure and configured size bounds."""
    limits = limits or FlowLimits()
    diagnostics: list[FlowDiagnostic] = []

    # Wire byte size ceiling check
    try:
        raw_bytes = json.dumps(source).encode("utf-8")
        if len(raw_bytes) > limits.max_flow_bytes:
            diagnostics.append(
                FlowDiagnostic(
                    code="SCHEMA_INVALID",
                    path="",
                    message=f"Flow exceeds maximum wire size of {limits.max_flow_bytes} bytes",
                )
            )
    except Exception as exc:
        diagnostics.append(
            FlowDiagnostic(
                code="SCHEMA_INVALID",
                path="",
                message=f"Flow JSON serialization error: {exc}",
            )
        )
        return diagnostics

    # Schema Draft 2020-12 validation
    errors = sorted(_authoring_validator.iter_errors(source), key=lambda e: list(e.path))
    for err in errors:
        pointer = "/" + "/".join(str(p) for p in err.path) if err.path else ""
        diagnostics.append(
            FlowDiagnostic(
                code="SCHEMA_INVALID",
                path=pointer,
                message=err.message,
            )
        )

    # Check bounds against limits if schema passed
    if not diagnostics:
        steps = source.get("steps", [])
        if len(steps) > limits.max_steps:
            diagnostics.append(
                FlowDiagnostic(
                    code="SCHEMA_INVALID",
                    path="/steps",
                    message=f"Step count {len(steps)} exceeds max_steps {limits.max_steps}",
                )
            )

    diagnostics.sort(key=lambda d: d.sort_key())
    return diagnostics


def normalize_executable(
    source: dict[str, Any],
    limits: FlowLimits | None = None,
) -> dict[str, Any]:
    """Fill documented defaults into authoring flow to produce normalized executable flow."""
    limits = limits or FlowLimits()
    if not source:
        return {"schema_version": 1, "steps": []}

    flow = copy.deepcopy(source)

    # Default behavior
    behavior_defaults = {
        "ordering": "adaptive",
        "allow_relevant_detours": True,
        "capture_early_information": True,
        "allow_corrections": True,
        "clarification_attempts": 2,
        "allow_opt_out": True,
    }
    existing_behavior = flow.get("behavior") or {}
    behavior = {**behavior_defaults, **existing_behavior}
    flow["behavior"] = behavior

    steps = flow.get("steps") or []
    for step in steps:
        stype = step.get("type")
        if "priority" not in step:
            step["priority"] = 100
        if "required" not in step:
            step["required"] = False if stype == "end" else True
        if "prerequisites" not in step:
            step["prerequisites"] = []
        if "captures" not in step:
            step["captures"] = []
        if "transitions" not in step:
            step["transitions"] = []
        if "messages" not in step and stype in ("ask", "collect", "action"):
            step["messages"] = []
        if "fallback" not in step and stype != "end":
            step["fallback"] = {
                "action": "clarify",
                "max_attempts": behavior.get("clarification_attempts", 2),
                "on_exhausted": "offer_exit",
            }

        # Normalize captures
        for cap in step.get("captures", []):
            if "required" not in cap:
                cap["required"] = True
            if "confirmation" not in cap:
                cap["confirmation"] = "validated"
            if "allow_refusal" not in cap:
                cap["allow_refusal"] = True
            kind = cap.get("kind")
            if kind == "string":
                if "max_length" not in cap:
                    cap["max_length"] = 512
            elif kind == "integer":
                if "minimum" not in cap:
                    cap["minimum"] = limits.min_safe_integer
                if "maximum" not in cap:
                    cap["maximum"] = limits.max_safe_integer
            elif kind == "positive_integer":
                if "maximum" not in cap:
                    cap["maximum"] = limits.max_safe_integer

    return flow


def validate_executable(
    flow: dict[str, Any],
    limits: FlowLimits | None = None,
    capabilities: AdapterCapabilities | None = None,
    tools: ToolRegistry | None = None,
) -> list[FlowDiagnostic]:
    """Validate executable structure, semantics, and capability requirements."""
    limits = limits or FlowLimits()
    diagnostics: list[FlowDiagnostic] = []

    # 1. Authoring check first
    auth_errors = validate_authoring(flow, limits)
    if auth_errors:
        return auth_errors

    # 2. Executable structure check
    exec_errors = list(_executable_validator.iter_errors(flow))
    if exec_errors:
        steps = flow.get("steps")
        if not steps:
            diagnostics.append(
                FlowDiagnostic(
                    code="INCOMPLETE_FLOW",
                    path="/steps",
                    message="Flow must contain at least one step",
                )
            )
            return diagnostics
        if not flow.get("entry_step_id") or not flow.get("exit_step_id"):
            diagnostics.append(
                FlowDiagnostic(
                    code="INCOMPLETE_FLOW",
                    path="",
                    message="Flow must specify entry_step_id and exit_step_id",
                )
            )
            return diagnostics

        for err in exec_errors:
            pointer = "/" + "/".join(str(p) for p in err.path) if err.path else ""
            if len(err.path) >= 3 and err.path[2] in ("title", "instruction"):
                diagnostics.append(
                    FlowDiagnostic(
                        code="INCOMPLETE_STEP",
                        path=pointer,
                        message=f"Step {err.path[2]} cannot be blank or whitespace-only",
                    )
                )
                continue
            if err.validator == "required" and any(k in err.message for k in ("'title'", "'instruction'")):
                diagnostics.append(
                    FlowDiagnostic(
                        code="INCOMPLETE_STEP",
                        path=pointer,
                        message=err.message,
                    )
                )
                continue
            if len(err.path) >= 4 and err.path[2] == "transitions":
                step_idx = err.path[1]
                trans_idx = err.path[3]
                trans = flow["steps"][step_idx]["transitions"][trans_idx]
                if "to" not in trans or not trans.get("to"):
                    diagnostics.append(
                        FlowDiagnostic(
                            code="INCOMPLETE_TRANSITION",
                            path=pointer,
                            message="Transition must specify a target 'to' step ID",
                        )
                    )
                    continue
            diagnostics.append(
                FlowDiagnostic(
                    code="SCHEMA_INVALID",
                    path=pointer,
                    message=err.message,
                )
            )
        diagnostics.sort(key=lambda d: d.sort_key())
        return diagnostics

    # 3. Semantic validation
    steps = flow.get("steps", [])
    step_map = {s["id"]: s for s in steps}
    entry_id = flow.get("entry_step_id")
    exit_id = flow.get("exit_step_id")

    # Step ID uniqueness
    seen_step_ids = set()
    for s_idx, s in enumerate(steps):
        sid = s.get("id")
        if sid in seen_step_ids:
            diagnostics.append(
                FlowDiagnostic(
                    code="DUPLICATE_STEP",
                    path=f"/steps/{s_idx}/id",
                    message=f"Duplicate step id '{sid}'",
                )
            )
        seen_step_ids.add(sid)

    # Entry and exit step existence
    if entry_id not in step_map:
        diagnostics.append(
            FlowDiagnostic(
                code="INCOMPLETE_FLOW",
                path="/entry_step_id",
                message=f"Entry step '{entry_id}' not found in steps",
            )
        )
    if exit_id not in step_map:
        diagnostics.append(
            FlowDiagnostic(
                code="INCOMPLETE_FLOW",
                path="/exit_step_id",
                message=f"Exit step '{exit_id}' not found in steps",
            )
        )
    elif step_map[exit_id].get("type") != "end":
        diagnostics.append(
            FlowDiagnostic(
                code="INCOMPLETE_STEP",
                path=f"/steps/{steps.index(step_map[exit_id])}/type",
                message=f"Exit step '{exit_id}' must be of type 'end'",
            )
        )

    # Collect fields and check field uniqueness
    field_map: dict[str, tuple[dict[str, Any], str]] = {}
    total_fields = 0
    for s_idx, s in enumerate(steps):
        # Check blank title or instruction
        title = s.get("title", "")
        if not title or not title.strip():
            diagnostics.append(
                FlowDiagnostic(
                    code="INCOMPLETE_STEP",
                    path=f"/steps/{s_idx}/title",
                    message="Step title cannot be blank",
                )
            )
        instruction = s.get("instruction", "")
        if not instruction or not instruction.strip():
            diagnostics.append(
                FlowDiagnostic(
                    code="INCOMPLETE_STEP",
                    path=f"/steps/{s_idx}/instruction",
                    message="Step instruction cannot be blank",
                )
            )

        # Captures
        captures = s.get("captures", [])
        if s.get("type") in ("ask", "collect") and len(captures) == 0:
            diagnostics.append(
                FlowDiagnostic(
                    code="INVALID_NODE",
                    path=f"/steps/{s_idx}/captures",
                    message="ask/collect steps require at least one capture",
                )
            )

        for c_idx, cap in enumerate(captures):
            total_fields += 1
            fid = cap.get("field")
            if fid:
                if fid in field_map:
                    diagnostics.append(
                        FlowDiagnostic(
                            code="DUPLICATE_FIELD",
                            path=f"/steps/{s_idx}/captures/{c_idx}/field",
                            message=f"Field '{fid}' already defined by step '{field_map[fid][1]}'",
                        )
                    )
                else:
                    field_map[fid] = (cap, s["id"])

        # Messages uniqueness
        messages = s.get("messages", [])
        seen_msg_ids = set()
        for m_idx, msg in enumerate(messages):
            mid = msg.get("id")
            if mid in seen_msg_ids:
                diagnostics.append(
                    FlowDiagnostic(
                        code="DUPLICATE_MESSAGE",
                        path=f"/steps/{s_idx}/messages/{m_idx}/id",
                        message=f"Duplicate message id '{mid}' within step '{s['id']}'",
                    )
                )
            seen_msg_ids.add(mid)

    if total_fields > limits.max_total_captured_fields:
        diagnostics.append(
            FlowDiagnostic(
                code="SCHEMA_INVALID",
                path="/steps",
                message=f"Total captured fields {total_fields} exceeds limit {limits.max_total_captured_fields}",
            )
        )

    # Welcome step verification
    welcome = flow.get("welcome")
    if welcome:
        q_step_id = welcome.get("question_step_id")
        if q_step_id not in step_map:
            diagnostics.append(
                FlowDiagnostic(
                    code="UNKNOWN_STEP",
                    path="/welcome/question_step_id",
                    message=f"Welcome question_step_id '{q_step_id}' does not exist",
                )
            )
        else:
            q_step = step_map[q_step_id]
            if q_step.get("type") not in ("ask", "collect"):
                diagnostics.append(
                    FlowDiagnostic(
                        code="INVALID_NODE",
                        path="/welcome/question_step_id",
                        message="Welcome question must target ask or collect step",
                    )
                )
            if q_step_id != entry_id:
                diagnostics.append(
                    FlowDiagnostic(
                        code="INVALID_NODE",
                        path="/welcome/question_step_id",
                        message="Welcome question must target entry step",
                    )
                )
            if q_step.get("prerequisites"):
                diagnostics.append(
                    FlowDiagnostic(
                        code="INVALID_NODE",
                        path="/welcome/question_step_id",
                        message="Welcome question step must be eligible in initial state (empty prerequisites)",
                    )
                )

    # Condition & transition validation per step
    for s_idx, s in enumerate(steps):
        stype = s.get("type")
        transitions = s.get("transitions", [])
        prereqs = s.get("prerequisites", [])

        # Step type requirements
        if stype in ("ask", "collect"):
            has_required_capture = any(c.get("required", True) for c in s.get("captures", []))
            if not has_required_capture:
                diagnostics.append(
                    FlowDiagnostic(
                        code="INVALID_NODE",
                        path=f"/steps/{s_idx}/captures",
                        message="ask/collect steps require at least one required capture",
                    )
                )
        if stype == "action":
            action_def = s.get("action")
            if not action_def or not action_def.get("tool_id"):
                diagnostics.append(
                    FlowDiagnostic(
                        code="INVALID_NODE",
                        path=f"/steps/{s_idx}/action",
                        message="action step requires tool_id",
                    )
                )
            else:
                tool_id = action_def.get("tool_id")
                registry = tools or ToolRegistry.empty()
                if not registry.is_registered(tool_id):
                    diagnostics.append(
                        FlowDiagnostic(
                            code="TOOL_UNQUALIFIED",
                            path=f"/steps/{s_idx}/action/tool_id",
                            message=f"Action step '{s.get('id')}' specifies tool '{tool_id}' not found in allowlisted tool registry",
                        )
                    )
                else:
                    diagnostics.append(
                        FlowDiagnostic(
                            code="ACTION_UNSUPPORTED",
                            path=f"/steps/{s_idx}/action",
                            message=f"Action step '{s.get('id')}' cannot be published: runtime action lifecycle (RequestAction / confirmation records) is not yet supported in this build",
                        )
                    )

        if stype == "end":
            if s.get("required") is True:
                diagnostics.append(
                    FlowDiagnostic(
                        code="INVALID_NODE",
                        path=f"/steps/{s_idx}/required",
                        message="end step must set required: false",
                    )
                )
            if len(transitions) > 0:
                diagnostics.append(
                    FlowDiagnostic(
                        code="INVALID_NODE",
                        path=f"/steps/{s_idx}/transitions",
                        message="end step cannot have transitions",
                    )
                )
            if len(prereqs) > 0:
                diagnostics.append(
                    FlowDiagnostic(
                        code="INVALID_NODE",
                        path=f"/steps/{s_idx}/prerequisites",
                        message="end step cannot have prerequisites",
                    )
                )

        # Transition validation
        else_count = 0
        has_non_refusal = False
        for t_idx, t in enumerate(transitions):
            target = t.get("to")
            if target and target not in step_map:
                diagnostics.append(
                    FlowDiagnostic(
                        code="MISSING_TARGET",
                        path=f"/steps/{s_idx}/transitions/{t_idx}/to",
                        message=f"Transition target '{target}' does not exist in steps",
                    )
                )

            when = t.get("when", {})
            op = when.get("operator")
            if op == "else":
                else_count += 1
                if t_idx != len(transitions) - 1:
                    diagnostics.append(
                        FlowDiagnostic(
                            code="INVALID_BRANCH",
                            path=f"/steps/{s_idx}/transitions/{t_idx}/when/operator",
                            message="ELSE transition must be the last row",
                        )
                    )
            elif op != "is_refused":
                has_non_refusal = True

            # Validate condition operator and field type
            _validate_condition(
                cond=when,
                path_prefix=f"/steps/{s_idx}/transitions/{t_idx}/when",
                field_map=field_map,
                step_map=step_map,
                step_type=stype,
                is_prereq=False,
                current_step_id=s["id"],
                diagnostics=diagnostics,
            )

        if else_count > 1:
            diagnostics.append(
                FlowDiagnostic(
                    code="INVALID_BRANCH",
                    path=f"/steps/{s_idx}/transitions",
                    message="At most one ELSE transition allowed per step",
                )
            )
        if else_count == 1 and len(transitions) == 1:
            diagnostics.append(
                FlowDiagnostic(
                    code="INVALID_BRANCH",
                    path=f"/steps/{s_idx}/transitions",
                    message="ELSE transition cannot be the sole row",
                )
            )

        if stype in ("ask", "collect") and len(transitions) > 0 and not has_non_refusal:
            diagnostics.append(
                FlowDiagnostic(
                    code="INVALID_BRANCH",
                    path=f"/steps/{s_idx}/transitions",
                    message="Capture nodes require at least one non-refusal route",
                )
            )

        # Prerequisites validation & satisfiability
        prereq_constraints: dict[str, list[dict[str, Any]]] = {}
        for p_idx, p in enumerate(prereqs):
            _validate_condition(
                cond=p,
                path_prefix=f"/steps/{s_idx}/prerequisites/{p_idx}",
                field_map=field_map,
                step_map=step_map,
                step_type=stype,
                is_prereq=True,
                current_step_id=s["id"],
                diagnostics=diagnostics,
            )
            fid = p.get("field")
            if fid:
                if fid not in prereq_constraints:
                    prereq_constraints[fid] = []
                prereq_constraints[fid].append(p)

        # Satisfiability check per field in prerequisites
        for fid, conds in prereq_constraints.items():
            if _is_unsatisfiable_field_constraints(conds):
                diagnostics.append(
                    FlowDiagnostic(
                        code="IMPOSSIBLE_PREREQUISITE",
                        path=f"/steps/{s_idx}/prerequisites",
                        message=f"Prerequisites for field '{fid}' are contradictory and cannot be satisfied",
                    )
                )

        # Message template validation
        for m_idx, msg in enumerate(s.get("messages", [])):
            for lang in ("en", "hi"):
                text = msg.get(lang, "")
                # Find placeholders like {{field_id}}
                import re
                placeholders = re.findall(r"\{\{([^}]+)\}\}", text)
                for ph in placeholders:
                    clean_ph = ph.strip()
                    if clean_ph not in field_map:
                        diagnostics.append(
                            FlowDiagnostic(
                                code="UNKNOWN_FIELD",
                                path=f"/steps/{s_idx}/messages/{m_idx}/{lang}",
                                message=f"Template placeholder '{clean_ph}' references undeclared field",
                            )
                        )

    # Graph cycle check (authoring transitions DAG)
    adj: dict[str, list[str]] = {s["id"]: [] for s in steps}
    for s in steps:
        for t in s.get("transitions", []):
            to = t.get("to")
            if to and to in adj:
                adj[s["id"]].append(to)

    visited: dict[str, int] = {}
    has_cycle = False

    def _dfs_cycle(node: str, cycle_path: list[str]) -> bool:
        visited[node] = 1
        for nxt in adj[node]:
            if visited.get(nxt) == 1:
                return True
            if visited.get(nxt) is None:
                if _dfs_cycle(nxt, cycle_path + [nxt]):
                    return True
        visited[node] = 2
        return False

    for sid in adj:
        if visited.get(sid) is None:
            if _dfs_cycle(sid, [sid]):
                has_cycle = True
                break

    if has_cycle:
        diagnostics.append(
            FlowDiagnostic(
                code="GRAPH_CYCLE",
                path="/steps",
                message="Graph transitions contain a directed cycle; graph must be acyclic",
            )
        )

    # Reachability from entry
    reachable = set()
    queue = [entry_id] if entry_id in step_map else []
    while queue:
        curr = queue.pop(0)
        if curr not in reachable:
            reachable.add(curr)
            for nxt in adj.get(curr, []):
                if nxt not in reachable:
                    queue.append(nxt)

    for s_idx, s in enumerate(steps):
        sid = s["id"]
        if sid != exit_id and sid not in reachable:
            diagnostics.append(
                FlowDiagnostic(
                    code="UNREACHABLE_STEP",
                    path=f"/steps/{s_idx}",
                    message=f"Step '{sid}' is unreachable from entry step '{entry_id}'",
                )
            )

    # Exit step reachability check
    if exit_id in step_map and exit_id not in reachable and entry_id != exit_id:
        diagnostics.append(
            FlowDiagnostic(
                code="UNREACHABLE_STEP",
                path=f"/steps/{steps.index(step_map[exit_id])}",
                message=f"Designated exit step '{exit_id}' is unreachable along authored normal transitions",
            )
        )

    # Transition warnings (OVERLAPPING_TRANSITIONS, SHADOWED_TRANSITION)
    for s_idx, s in enumerate(steps):
        transitions = s.get("transitions", [])
        if len(transitions) >= 2:
            seen_when = []
            for t_idx, t in enumerate(transitions):
                when = t.get("when", {})
                w_str = json.dumps(when, sort_keys=True)
                if w_str in seen_when:
                    diagnostics.append(
                        FlowDiagnostic(
                            code="OVERLAPPING_TRANSITIONS",
                            path=f"/steps/{s_idx}/transitions/{t_idx}",
                            message=f"Transition guard shadows or overlaps preceding guard in step '{s['id']}'",
                            severity="warning",
                        )
                    )
                seen_when.append(w_str)

    # Mode-specific capability checks
    if capabilities is not None:
        if capabilities.playback == "unqualified":
            diagnostics.append(
                FlowDiagnostic(
                    code="PLAYBACK_UNQUALIFIED",
                    path="",
                    message=f"Adapter '{capabilities.adapter_id}' playback is unqualified for voice execution",
                )
            )

    diagnostics.sort(key=lambda d: d.sort_key())
    return diagnostics


def _validate_condition(
    cond: dict[str, Any],
    path_prefix: str,
    field_map: dict[str, tuple[dict[str, Any], str]],
    step_map: dict[str, dict[str, Any]],
    step_type: str,
    is_prereq: bool,
    current_step_id: str,
    diagnostics: list[FlowDiagnostic],
) -> None:
    op = cond.get("operator")
    if not op:
        return

    if op == "completed":
        if is_prereq:
            diagnostics.append(
                FlowDiagnostic(
                    code="CONDITION_TYPE",
                    path=f"{path_prefix}/operator",
                    message="'completed' operator is allowed only on transitions, not prerequisites",
                )
            )
        return

    if op == "step_completed":
        ref_step = cond.get("step_id")
        if ref_step and ref_step not in step_map:
            diagnostics.append(
                FlowDiagnostic(
                    code="UNKNOWN_STEP",
                    path=f"{path_prefix}/step_id",
                    message=f"step_completed references unknown step '{ref_step}'",
                )
            )
        return

    if op == "all_applicable_required_fields_present":
        if step_type != "summary":
            diagnostics.append(
                FlowDiagnostic(
                    code="CONDITION_TYPE",
                    path=f"{path_prefix}/operator",
                    message="all_applicable_required_fields_present is allowed only on summary steps",
                )
            )
        # Cannot be on owner of any capture it aggregates
        current_step = step_map.get(current_step_id, {})
        if len(current_step.get("captures", [])) > 0:
            diagnostics.append(
                FlowDiagnostic(
                    code="CONDITION_TYPE",
                    path=f"{path_prefix}/operator",
                    message="all_applicable_required_fields_present cannot be placed on a step that defines captures",
                )
            )
        return

    if op == "else":
        return

    # Field-based conditions
    f_name = cond.get("field")
    if not f_name:
        return
    if f_name not in field_map:
        diagnostics.append(
            FlowDiagnostic(
                code="UNKNOWN_FIELD",
                path=f"{path_prefix}/field",
                message=f"Condition references unknown field '{f_name}'",
            )
        )
        return

    cap_def = field_map[f_name][0]
    kind = cap_def.get("kind")
    val = cond.get("value")

    # Ordered comparison checks
    if op in ("gt", "gte", "lt", "lte"):
        if kind not in ("integer", "positive_integer"):
            diagnostics.append(
                FlowDiagnostic(
                    code="CONDITION_TYPE",
                    path=f"{path_prefix}/operator",
                    message=f"Ordered operator '{op}' applies only to integer/positive_integer fields, got '{kind}'",
                )
            )
        if isinstance(val, bool) or not isinstance(val, int):
            diagnostics.append(
                FlowDiagnostic(
                    code="CONDITION_TYPE",
                    path=f"{path_prefix}/value",
                    message=f"Ordered operator '{op}' requires integer value",
                )
            )

    # Equality and membership checks
    if op in ("eq", "ne"):
        if kind == "enum":
            allowed_values = cap_def.get("values", [])
            if val not in allowed_values:
                diagnostics.append(
                    FlowDiagnostic(
                        code="CONDITION_TYPE",
                        path=f"{path_prefix}/value",
                        message=f"Value '{val}' is not in enum values {allowed_values}",
                    )
                )
        elif kind == "boolean":
            if not isinstance(val, bool):
                diagnostics.append(
                    FlowDiagnostic(
                        code="CONDITION_TYPE",
                        path=f"{path_prefix}/value",
                        message=f"Boolean field '{f_name}' requires boolean comparison value",
                    )
                )
        elif kind in ("integer", "positive_integer"):
            if isinstance(val, bool) or not isinstance(val, int):
                diagnostics.append(
                    FlowDiagnostic(
                        code="CONDITION_TYPE",
                        path=f"{path_prefix}/value",
                        message=f"Integer field '{f_name}' requires integer comparison value",
                    )
                )

    if op == "in":
        vals = cond.get("values", [])
        if kind == "enum":
            allowed_values = set(cap_def.get("values", []))
            for v_idx, v in enumerate(vals):
                if v not in allowed_values:
                    diagnostics.append(
                        FlowDiagnostic(
                            code="CONDITION_TYPE",
                            path=f"{path_prefix}/values/{v_idx}",
                            message=f"Value '{v}' is not in enum values {cap_def.get('values', [])}",
                        )
                    )


def _is_unsatisfiable_field_constraints(conds: list[dict[str, Any]]) -> bool:
    """Deterministic satisfiability check for a list of prerequisite atoms on a single field."""
    eq_val = None
    has_eq = False
    excluded_vals = set()
    allowed_set = None

    lower_bound = MIN_SAFE_INTEGER
    upper_bound = MAX_SAFE_INTEGER
    has_range = False

    is_present_required = False
    is_refused_required = False
    is_unknown_required = False
    is_candidate_required = False

    for c in conds:
        op = c.get("operator")
        val = c.get("value")

        if op == "eq":
            if has_eq and eq_val != val:
                return True
            has_eq = True
            eq_val = val
            is_present_required = True
        elif op == "ne":
            excluded_vals.add(val)
        elif op == "in":
            vals = set(c.get("values", []))
            if allowed_set is None:
                allowed_set = vals
            else:
                allowed_set = allowed_set.intersection(vals)
                if not allowed_set:
                    return True
            is_present_required = True
        elif op == "gt":
            has_range = True
            is_present_required = True
            lower_bound = max(lower_bound, val + 1)
        elif op == "gte":
            has_range = True
            is_present_required = True
            lower_bound = max(lower_bound, val)
        elif op == "lt":
            has_range = True
            is_present_required = True
            upper_bound = min(upper_bound, val - 1)
        elif op == "lte":
            has_range = True
            is_present_required = True
            upper_bound = min(upper_bound, val)
        elif op == "is_present":
            is_present_required = True
        elif op == "is_refused":
            is_refused_required = True
        elif op == "is_unknown":
            is_unknown_required = True
        elif op == "is_candidate":
            is_candidate_required = True

    # State contradiction checks
    state_count = sum([is_present_required, is_refused_required, is_unknown_required, is_candidate_required])
    if state_count > 1:
        return True

    # eq vs ne / in / range checks
    if has_eq:
        if eq_val in excluded_vals:
            return True
        if allowed_set is not None and eq_val not in allowed_set:
            return True
        if has_range and isinstance(eq_val, (int, float)):
            if eq_val < lower_bound or eq_val > upper_bound:
                return True

    if has_range and lower_bound > upper_bound:
        return True

    return False
