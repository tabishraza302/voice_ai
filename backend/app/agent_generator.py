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

### Conversational Workflow & Routing
[Analyze whether the use case is LINEAR (a single sequential flow) or BRANCHED (multiple intents, triage, or conditional decisions).
- IF LINEAR (e.g. simple reminder, sequential intake, short survey):
  Detail a clean sequential step-by-step list (Step 1 -> Step 2 -> Step 3). DO NOT invent fake intents or artificial branches.
- IF BRANCHED (e.g. multiple services, triage, or if/then choices):
  Structure with numbered steps and explicit routing rules:
  - Step 1 (Triage): If X route to Step 2, if Y route to Step 3.
  - Path Exits: each path exits with an outcome or joins a common closing step so parallel paths do not cross.]
### Information to Collect & Field Keys
[List specific fields or decisions to obtain with snake_case keys like caller_intent, customer_name, order_id]

### Tone & Style Guidelines
[Describe voice-friendly speaking style: brief sentences, natural transitions, empathetic and professional tone, avoid robotic jargon]

### Edge Cases & Fallbacks
[Instructions for handling confusion, angry callers, out-of-scope requests, and human transfer]

Do not include any conversational filler or preambles outside these sections."""


_GENERATE_SYSTEM_PROMPT = """You are an expert voice AI architect.
Given a user request or prompt, generate a fully configured voice agent specification with goal-based information fields and completion outcomes.
You MUST output ONLY a valid raw JSON object matching this schema:
{
  "name": "Short, human-friendly agent name (max 120 chars)",
  "description": "Brief 1-2 sentence description (max 500 chars)",
  "instructions": "Comprehensive system prompt for the voice AI (persona, tone, speaking style, concise answers, rules, edge cases)",
  "fields": [
    {
      "field_key": "unique_snake_case_key",
      "description": "Short description of what to collect",
      "guidance": "Instruction on how to ask the caller for this information",
      "options": ["optional_enum_1", "optional_enum_2"],
      "required": true
    }
  ],
  "outcomes": [
    {
      "label": "outcome_label",
      "required_fields": ["field_key_1", "field_key_2"]
    }
  ]
}

Guidelines for Goal-Based Configuration:
- Generate 2 to 6 information fields representing what the agent needs to collect.
- Mark primary triage/intent fields as required: true.
- For fields with defined choices, include non-empty options array. Free-text fields have options: [].
- Generate at least 1 outcome with the required_fields that mark it complete.
- Do NOT output any markdown ticks (like ```json), commentary, or text outside the raw JSON object."""

def get_openai_client(model_override: str | None = None) -> tuple[AsyncOpenAI, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(503, "OpenAI is unavailable because OPENAI_API_KEY is not configured on the server.")

    models_env = os.environ.get("VERIFIED_OPENAI_MODELS", "").strip()
    verified_models = [m.strip() for m in models_env.split(",") if m.strip()] if models_env else []

    if model_override and model_override.strip():
        chosen = model_override.strip()
        if verified_models and chosen not in verified_models:
            raise HTTPException(422, f"Model '{chosen}' is not in the server allowlist.")
        model = chosen
    elif verified_models:
        model = verified_models[0]
    else:
        model = "gpt-4o-mini"

    return AsyncOpenAI(api_key=api_key, timeout=60.0), model


async def enhance_prompt(prompt: str, model_name: str | None = None) -> str:
    cleaned = prompt.strip()
    if not cleaned:
        raise HTTPException(422, "Prompt cannot be empty.")
    if len(cleaned) < 10:
        raise HTTPException(422, "Prompt must be at least 10 characters long.")

    client, model = get_openai_client(model_name)
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
    model_name: str | None = None,
) -> AsyncIterator[str]:
    from .agents import Agent, generate_slug
    from .fields import AgentField, AgentOutcome
    cleaned = prompt.strip()
    if not cleaned:
        yield _format_sse("error", {"message": "Prompt cannot be empty."})
        return
    if len(cleaned) < 10:
        yield _format_sse("error", {"message": "Prompt must be at least 10 characters long."})
        return

    try:
        client, model = get_openai_client(model_name)
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
            "fields": [
                {"field_key": "caller_intent", "description": "Caller's request", "guidance": "Ask how you can help.", "required": True, "options": []},
                {"field_key": "details", "description": "Request details", "guidance": "Gather details from caller.", "required": False, "options": []},
            ],
            "outcomes": [
                {"label": "request_completed", "required_fields": ["caller_intent", "details"]}
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
        llm_provider="openai",
        llm_model=model,
        status="draft",
    )
    db.add(agent)

    raw_fields = parsed.get("fields") or parsed.get("steps") or []
    if not isinstance(raw_fields, list) or not raw_fields:
        raw_fields = [
            {"field_key": "caller_intent", "description": "Caller's request", "guidance": "Ask how you can help.", "required": True, "options": []},
            {"field_key": "details", "description": "Request details", "guidance": "Gather details from caller.", "required": False, "options": []},
        ]

    seen_keys: set[str] = set()
    created_fields: list[AgentField] = []
    for index, f_data in enumerate(raw_fields):
        if not isinstance(f_data, dict):
            f_data = {}
        raw_key = re.sub(r"[^a-zA-Z0-9_]+", "_", str(f_data.get("field_key") or f"field_{index+1}")).strip("_").lower()
        if not raw_key:
            raw_key = f"field_{index+1}"
        if raw_key in seen_keys:
            raw_key = f"{raw_key}_{index+1}"
        seen_keys.add(raw_key)

        title = str(f_data.get("description") or f_data.get("title") or f"Field {index+1}").strip()[:255]
        guidance = str(f_data.get("guidance") or f_data.get("objective") or title).strip()
        options = [str(opt).strip() for opt in f_data.get("options") or () if str(opt).strip()]
        required = bool(f_data.get("required", index == 0))

        field_model = AgentField(
            id=uuid.uuid4(),
            agent_id=agent_id,
            field_key=raw_key,
            description=title,
            guidance=guidance,
            required=required,
            options=options,
            required_when=[],
            sort_order=index,
        )
        db.add(field_model)
        created_fields.append(field_model)

    raw_outcomes = parsed.get("outcomes") or []
    if isinstance(raw_outcomes, list):
        for o_data in raw_outcomes:
            if not isinstance(o_data, dict):
                continue
            label = re.sub(r"[^a-zA-Z0-9_]+", "_", str(o_data.get("label") or "completed")).strip("_").lower() or "completed"
            req_fields = [
                str(rf).strip()
                for rf in o_data.get("required_fields") or ()
                if str(rf).strip() in seen_keys
            ]
            outcome_model = AgentOutcome(
                id=uuid.uuid4(),
                agent_id=agent_id,
                label=label,
                conditions=[],
                required_fields=req_fields,
            )
            db.add(outcome_model)

    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        yield _format_sse("error", {"message": f"Database commit failed: {str(exc)}"})
        return

    yield _format_sse("complete", {"agent_id": str(agent_id), "name": agent_name})
