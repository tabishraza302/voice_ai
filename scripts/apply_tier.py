#!/usr/bin/env python3
"""Apply the measured tier decision to ``.env.example``.

The tier rule lives in ``scripts/memory_baseline.sh``, which writes
``data/memory_baseline.json``. This script reads that file and updates
the two values in ``.env.example`` that depend on it:

* ``llm_model`` — which LLM tier the measurement selected;
* ``ttfs_p99_latency`` — the measured STT latency from
  ``data/stt_bench.json``, if that has been run.

Everything else in ``.env.example`` is a hand-written default and is
left alone: this script only touches what a measurement decided.

Usage:
    uv run python scripts/apply_tier.py
    uv run python scripts/apply_tier.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "data" / "memory_baseline.json"
BENCH_PATH = REPO_ROOT / "data" / "stt_bench.json"
ENV_PATH = REPO_ROOT / ".env.example"


def set_env_value(text: str, key: str, value: str) -> tuple[str, bool]:
    """Replace `key=value` in an env file, keeping the comment above it."""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if not pattern.search(text):
        return text, False
    replaced, count = pattern.subn(f"{key}={value}", text)
    return replaced, count == 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not BASELINE_PATH.exists():
        print(f"missing {BASELINE_PATH}; run scripts/memory_baseline.sh first", file=sys.stderr)
        return 1

    baseline = json.loads(BASELINE_PATH.read_text())
    llm_model = baseline.get("llm_model")
    if not llm_model:
        print("the baseline has no llm_model", file=sys.stderr)
        return 1

    updates: dict[str, str] = {"llm_model": llm_model}

    # The STT fallback, when the measurement selected the 8-bit model.
    stt_fallback = baseline.get("stt_fallback_model")
    if stt_fallback:
        updates["stt_model"] = stt_fallback

    # The measured STT latency, when the benchmark has been run.
    if BENCH_PATH.exists():
        bench = json.loads(BENCH_PATH.read_text())
        p99 = bench.get("ttfs_p99_worst")
        if p99:
            updates["ttfs_p99_latency"] = f"{p99}"

    text = ENV_PATH.read_text()
    changed = []
    for key, value in updates.items():
        text, ok = set_env_value(text, key, value)
        if ok:
            changed.append(f"{key}={value}")

    if args.dry_run:
        print("would update .env.example:")
        for item in changed:
            print(f"  {item}")
        return 0

    ENV_PATH.write_text(text)
    print("updated .env.example:")
    for item in changed:
        print(f"  {item}")
    print(
        f"\ntier: {llm_model} "
        f"(available for the LLM: {baseline.get('available_for_llm_gb')} GB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
