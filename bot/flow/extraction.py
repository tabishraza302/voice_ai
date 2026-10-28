"""Pure, allowlisted deterministic extraction for user input.

Per flow-contract.md section 1:
- Zero model inference, GPU, network, or provider imports.
- Pure string/regex extraction for supported scalar types and enums.
- Stop and refusal detection.
"""

from __future__ import annotations

import re
from typing import Any

from bot.flow.schemas import (
    CandidateCaptureUpdate,
    CaptureUpdate,
    Evidence,
    ProposeCaptureUpdate,
    RefuseCaptureUpdate,
    Scalar,
)

REFUSAL_PATTERNS = [
    r"\bdo not want to share\b",
    r"\bdon't want to share\b",
    r"\bprefer not to say\b",
    r"\brefuse\b",
    r"\bwon't tell\b",
    r"\bwill not tell\b",
    r"\bनहीं बताना\b",
    r"\bसाझा नहीं करना\b",
]

STOP_PATTERNS = [
    r"\bstop\b",
    r"\bstop now\b",
    r"\bquit\b",
    r"\bexit\b",
    r"\bcancel\b",
    r"\bरुको\b",
    r"\bबंद करो\b",
]


def is_refusal_text(text: str) -> bool:
    """Check if user text indicates refusal to answer."""
    lower = text.lower().strip()
    return any(re.search(pat, lower) for pat in REFUSAL_PATTERNS)


def is_stop_text(text: str) -> bool:
    """Check if user text indicates an explicit stop/abort."""
    lower = text.lower().strip()
    return any(re.search(pat, lower) for pat in STOP_PATTERNS)


def extract_deterministic_updates(
    text: str,
    turn_id: str,
    captures_definition: list[dict[str, Any]],
    locale: str = "en",
) -> list[CaptureUpdate]:
    """Purely extract allowlisted deterministic updates from text for declared captures."""
    updates: list[CaptureUpdate] = []
    lower = text.lower()

    # Check refusal first
    if is_refusal_text(text):
        for cap in captures_definition:
            updates.append(
                RefuseCaptureUpdate(
                    field=cap["field"],
                    evidence=Evidence(source_turn_id=turn_id, quote=text.strip()),
                )
            )
        return updates

    for cap in captures_definition:
        fid = cap["field"]
        kind = cap["kind"]

        if kind == "enum":
            values = cap.get("values", [])
            for val in values:
                # Direct match of normalized enum or hyphen/space variant
                val_pattern = re.escape(val.replace("_", " "))
                match = re.search(r"\b" + val_pattern + r"\b", lower)
                if match:
                    quote = text[match.start() : match.end()]
                    updates.append(
                        ProposeCaptureUpdate(
                            field=fid,
                            value=val,
                            evidence=Evidence(source_turn_id=turn_id, quote=quote),
                        )
                    )
                    break

        elif kind in ("integer", "positive_integer"):
            # Extract currency or integer tokens like 80000, 80,000, 5000
            matches = list(re.finditer(r"\b(\d[\d,]*)\b", text))
            if matches:
                # If ambiguous period or multiple numbers, candidate
                if len(matches) > 1:
                    raw_val = int(matches[0].group(1).replace(",", ""))
                    updates.append(
                        CandidateCaptureUpdate(
                            field=fid,
                            value=raw_val,
                            reason="ambiguous",
                            evidence=Evidence(source_turn_id=turn_id, quote=matches[0].group(0)),
                        )
                    )
                else:
                    raw_val = int(matches[0].group(1).replace(",", ""))
                    updates.append(
                        ProposeCaptureUpdate(
                            field=fid,
                            value=raw_val,
                            evidence=Evidence(source_turn_id=turn_id, quote=matches[0].group(0)),
                        )
                    )

        elif kind == "boolean":
            yes_match = re.search(r"\b(yes|yeah|sure|true|हाँ|सही)\b", lower)
            no_match = re.search(r"\b(no|nope|false|नहीं|गलत)\b", lower)
            if yes_match and not no_match:
                updates.append(
                    ProposeCaptureUpdate(
                        field=fid,
                        value=True,
                        evidence=Evidence(source_turn_id=turn_id, quote=yes_match.group(0)),
                    )
                )
            elif no_match and not yes_match:
                updates.append(
                    ProposeCaptureUpdate(
                        field=fid,
                        value=False,
                        evidence=Evidence(source_turn_id=turn_id, quote=no_match.group(0)),
                    )
                )

    return updates
