import os
from fastapi import HTTPException
from openai import AsyncOpenAI

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

    return AsyncOpenAI(api_key=api_key, timeout=45.0), model


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
