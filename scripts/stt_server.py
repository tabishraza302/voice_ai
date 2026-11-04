#!/usr/bin/env python3
"""Minimal passthrough to mlx-audio's server (STT + TTS).

The voice-only refactor removed the streaming-lookahead patching. This
module just hands off to ``mlx_audio.server.main()`` with the flags the
caller passes (``--host``, ``--port``, ...).

Usage:
    uv run python scripts/stt_server.py --host 127.0.0.1 --port 8020
"""

from __future__ import annotations


def main() -> None:
    import mlx_audio.server as server

    server.main()


if __name__ == "__main__":
    main()
