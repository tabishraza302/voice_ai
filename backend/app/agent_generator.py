import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from fastapi import HTTPException
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

_ENHANCE_SYSTEM_PROMPT = """You are an expert prompt engineer specializing in voice AI agents.
Your task is to take a user's rough idea or prompt for a voice agent and rewrite it into a clear, professional, and comprehensive voice agent prompt.

You MUST format your output using the following markdown headings exactly:
### Role & Persona
[Define who the agent is, their name/identity, personality, and relationship to the caller]

### Primary Objective
[Define the main goals of the conversation clearly and concisely]

### Information to Collect
[List the specific fields, customer details, or decisions the agent needs to obtain from the caller]

### Tone & Style Guidelines
[Describe voice-friendly speaking style: brief sentences, natural transitions, empathetic and professional tone, avoid robotic jargon]

### Edge Cases & Fallbacks
[Instructions for handling confusion, angry callers, out-of-scope requests, and human transfer]

Do not include any conversational filler or preambles outside these sections."""


_GENERATE_SYSTEM_PROMPT = """You are an expert voice AI architect.
Given a user request or prompt, generate a fully configured voice agent specification and call flow steps.
You MUST output ONLY a valid JSON object matching this schema:
{
  "name": "Short, human-friendly agent name (max 120 chars)",
  "description": "Brief 1-2 sentence description (max 500 chars)",
  "instructions": "Comprehensive system prompt for the voice AI (persona, tone, speaking style, concise answers, rules, edge cases)",
  "steps": [
    {
      "field_key": "unique_snake_case_key",
      "title": "Clear step title",
      "objective": "Clear instructions for what the agent should accomplish and collect in this turn/step",
      "branches": [
        {
          "target_step_index": 1,
          "outcome": "optional outcome label (e.g. transfer, completed)",
          "conditions": []
        }
      ]
    }
  ]
}

Guidelines:
- Generate between 2 and 6 sequential steps representing the conversation flow (e.g. greeting, identifying purpose/user, gathering details, confirming action, wrap up).
- Each step's field_key MUST be unique, concise, and snake_case (e.g. caller_name, appointment_date, issue_type).
- If the request requires conditional branching (e.g. if new customer vs existing, or if reschedule vs cancel), set target_step_index to the 0-based integer index of the target step.
- Otherwise, branches can be empty or omit target_step_index to proceed linearly to the next step.
- Do NOT output any markdown ticks (like ```json), commentary, or text outside the raw JSON object."""


def get_openai_client() -> tuple[AsyncOpenAI, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(503, "OpenAI is unavailable because OPENAI_API_KEY is not configured on the server.")

    models_env = os.environ.get("VERIFIED_OPENAI_MODELS", "").strip()
    if models_env:
        models = [m.strip() for m in models_env.split(",") if m.strip()]
        model = models[0] if models else "gpt-4o-mini"
    else:
        model = "gpt-4o-mini"

    return AsyncOpenAI(api_key=api_key, timeout=60.0), model


async def enhance_prompt(prompt: str) -> str:
    cleaned = prompt.strip()
    if not cleaned:
        raise HTTPException(422, "Prompt cannot be empty.")
    if len(cleaned) < 10:
        raise HTTPException(422, "Prompt must be at least 10 characters long.")

    client, model = get_openai_client()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _ENHANCE_SYSTEM_PROMPT},
                {"role": "user", "content": cleaned},
            ],
            temperature=0.7,
        )
        content = response.choices[0].message.content
        if not content:
            raise HTTPException(502, "OpenAI returned an empty response.")
        return content.strip()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Failed to enhance prompt with OpenAI: {str(exc)}") from exc


def _format_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def stream_agent_generation(
    prompt: str,
    db: AsyncSession,
    owner_id: uuid.UUID,
) -> AsyncIterator[str]:
    from .agents import Agent, generate_slug
    from .steps import Step

    cleaned = prompt.strip()
    if not cleaned:
        yield _format_sse("error", {"message": "Prompt cannot be empty."})
        return
    if len(cleaned) < 10:
        yield _format_sse("error", {"message": "Prompt must be at least 10 characters long."})
        return

    try:
        client, model = get_openai_client()
    except HTTPException as e:
        yield _format_sse("error", {"message": e.detail})
        return

    yield _format_sse("milestone", {"phase": "analyzing"})

    full_text = ""
    instruction_milestone_sent = False
    call_flow_milestone_sent = False

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _GENERATE_SYSTEM_PROMPT},
                {"role": "user", "content": cleaned},
            ],
            temperature=0.7,
            stream=True,
        )

        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            delta_content = getattr(delta, "content", None)
            if delta_content:
                full_text += delta_content
                yield _format_sse("token", {"text": delta_content})

                if not instruction_milestone_sent and '"instructions"' in full_text:
                    instruction_milestone_sent = True
                    yield _format_sse("milestone", {"phase": "instructions"})

                if not call_flow_milestone_sent and '"steps"' in full_text:
                    call_flow_milestone_sent = True
                    yield _format_sse("milestone", {"phase": "call_flow"})

    except Exception as exc:
        yield _format_sse("error", {"message": f"OpenAI streaming failed: {str(exc)}"})
        return

    yield _format_sse("milestone", {"phase": "saving"})

    raw_json = full_text.strip()
    if raw_json.startswith("```"):
        raw_json = re.sub(r"^```[a-zA-Z]*\n", "", raw_json)
        raw_json = re.sub(r"\n```$", "", raw_json).strip()

    try:
        parsed = json.loads(raw_json)
        if not isinstance(parsed, dict):
            raise ValueError("Expected JSON object")
    except Exception:
        name_match = re.search(r'"name":\s*"([^"]+)"', full_text)
        name = name_match.group(1) if name_match else "AI Assistant"
        parsed = {
            "name": name,
            "description": f"Generated agent for: {cleaned[:80]}",
            "instructions": cleaned,
            "steps": [
                {"field_key": "greeting", "title": "Greeting & Identification", "objective": "Greet the caller and ask how to help."},
                {"field_key": "details", "title": "Collect Details", "objective": "Collect required information from the caller."},
                {"field_key": "wrapup", "title": "Confirmation & Closing", "objective": "Confirm the request and politely end the call."}
            ]
        }

    agent_name = str(parsed.get("name") or "AI Assistant").strip()[:120]
    description = str(parsed.get("description") or "").strip()[:500] or None
    instructions = str(parsed.get("instructions") or cleaned).strip()

    agent_id = uuid.uuid4()
    agent = Agent(
        id=agent_id,
        owner_id=owner_id,
        name=agent_name,
        slug=generate_slug(agent_name),
        description=description,
        instructions=instructions,
        languages=["en"],
        default_language="en",
        status="draft",
    )
    db.add(agent)

    raw_steps = parsed.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raw_steps = [
            {"field_key": "greeting", "title": "Greeting & Purpose", "objective": "Greet caller and determine goal."},
            {"field_key": "collect_info", "title": "Collect Information", "objective": "Gather details from caller."},
            {"field_key": "conclusion", "title": "Closing", "objective": "Confirm details and conclude call."}
        ]

    step_ids = [uuid.uuid4() for _ in raw_steps]
    seen_keys: set[str] = set()

    created_steps: list[Step] = []
    for index, s_data in enumerate(raw_steps):
        if not isinstance(s_data, dict):
            s_data = {"field_key": f"step_{index+1}", "title": f"Step {index+1}", "objective": f"Step {index+1}"}

        raw_key = re.sub(r"[^a-zA-Z0-9_]+", "_", str(s_data.get("field_key") or f"step_{index+1}")).strip("_").lower()
        if not raw_key:
            raw_key = f"step_{index+1}"
        if raw_key in seen_keys:
            raw_key = f"{raw_key}_{index+1}"
        seen_keys.add(raw_key)

        title = str(s_data.get("title") or f"Step {index+1}").strip()[:255] or f"Step {index+1}"
        objective = str(s_data.get("objective") or title).strip() or title

        sanitized_branches = []
        branches_input = s_data.get("branches")
        if isinstance(branches_input, list):
            for b in branches_input:
                if not isinstance(b, dict):
                    continue
                target_idx = b.get("target_step_index")
                target_id = None
                if isinstance(target_idx, int) and 0 <= target_idx < len(raw_steps) and target_idx != index:
                    target_id = str(step_ids[target_idx])

                sanitized_conditions = []
                conditions_input = b.get("conditions") if isinstance(b.get("conditions"), list) else []
                op_map = {
                    "=": "eq", "==": "eq", "eq": "eq",
                    "!=": "ne", "<>": "ne", "ne": "ne",
                    ">": "gt", "gt": "gt",
                    ">=": "gte", "gte": "gte",
                    "<": "lt", "lt": "lt",
                    "<=": "lte", "lte": "lte",
                    "in": "in",
                }
                for c in conditions_input:
                    if not isinstance(c, dict):
                        continue
                    f_key = c.get("field_key") or c.get("field")
                    field_name = str(f_key).strip() if f_key else raw_key
                    raw_op = str(c.get("operator") or "eq").strip().lower()
                    operator = op_map.get(raw_op, "eq")
                    operand = c.get("operand") if "operand" in c else c.get("value", True)
                    if operand is None:
                        operand = True
                    sanitized_conditions.append({
                        "field_key": field_name,
                        "operator": operator,
                        "operand": operand,
                    })

                outcome = str(b.get("outcome")).strip() if b.get("outcome") is not None else None
                if target_id is not None:
                    outcome = None
                elif not outcome:
                    # Neither a valid target nor an outcome: drop branch
                    continue

                sanitized_branches.append({
                    "conditions": sanitized_conditions,
                    "target_step_id": target_id,
                    "outcome": outcome,
                })

        step = Step(
            id=step_ids[index],
            agent_id=agent_id,
            field_key=raw_key,
            sort_order=index,
            title=title,
            objective=objective,
            branches=sanitized_branches,
        )
        db.add(step)
        created_steps.append(step)

    from .steps import validate_publish
    if validate_publish(agent, created_steps):
        for step in created_steps:
            step.branches = []

    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        yield _format_sse("error", {"message": f"Database commit failed: {str(exc)}"})
        return

    yield _format_sse("complete", {"agent_id": str(agent_id), "name": agent_name})
