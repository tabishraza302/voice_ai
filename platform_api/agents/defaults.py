"""Platform-owned default prompt and agent template definitions.

These are platform artifacts, not test fixtures or documentation examples.
They are loaded from package data so the control plane never reads
``tests/`` or ``docs/`` at runtime.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from bot.flow.canonical import POLICY_PREFIX

_DATA_DIR = Path(__file__).resolve().parent / "data"

DEFAULT_BASE_PROMPT = """You are a conversational voice AI assistant.
Your answers will be spoken aloud, so follow these speech guidelines:
- Be concise, direct, and conversational.
- Do not use markdown, bullet points, asterisks, citations, or emoji.
- Answer in 1 to 3 natural spoken sentences unless the user explicitly requests a longer explanation.
- For Hindi queries, respond naturally in Hindi. For English queries, respond in English.
- Avoid repeating the user's question back to them.
"""

DEFAULT_PERSONA_PROMPT = (
    "You are a friendly, helpful voice assistant. "
    "Respond with brevity and natural speech rhythm."
)

DEFAULT_POLICY_SLUG = "default-base"
DEFAULT_POLICY_VERSION = 1


def compute_policy_hash(prompt_text: str) -> str:
    """Compute the canonical policy revision hash used by the flow compiler."""
    return hashlib.sha256(POLICY_PREFIX + prompt_text.encode("utf-8")).hexdigest()


def compute_base_prompt_hash(prompt_text: str) -> str:
    """Compute deterministic SHA-256 hash representing base prompt content."""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()

def _load_flow(filename: str) -> dict[str, Any]:
    path = _DATA_DIR / filename
    return json.loads(path.read_text(encoding="utf-8"))


DEFAULT_AGENT_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "slug": "loan-qualification",
        "name": "Loan Qualification (EN/HI)",
        "description": (
            "Automated dual-language loan eligibility and document collection flow."
        ),
        "flow": _load_flow("loan-qualification.json"),
    },
    {
        "slug": "credit-card-qualification",
        "name": "Credit Card Qualification (EN/HI)",
        "description": (
            "Dual-language income check and credit card qualification flow."
        ),
        "flow": _load_flow("credit-card-qualification.json"),
    },
)

