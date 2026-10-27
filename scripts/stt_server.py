#!/usr/bin/env python3
"""Run mlx-audio's server with this deployment's chosen STT lookahead.

The server itself has no knob for the served Nemotron checkpoint: its
``--realtime-transcription-delay-ms`` flag is gated on the
``nemotron_asr_streaming`` model type, which this checkpoint does not use.
The lookahead still has to be chosen somewhere, and it belongs to the model
deployment, not to the agent: the endpoint, the wire protocol and the
pipeline are unchanged.

So the choice lives in ``.env`` as ``STT_LOOKAHEAD_FRAMES`` and is applied
here to each new ``/v1/realtime`` session — the seam every streaming session
goes through. The loaded model is not modified, so batch transcription and
the README's batch WER table keep the checkpoint default. A blank value keeps
that default for streaming too. See ``scripts/stt_lookahead.py`` for why the
override is safe or refused, and the README for the measured quality/latency
tradeoff.

Usage (the launcher passes the server's own flags through):

    uv run python scripts/stt_server.py --host 127.0.0.1 --port 8020
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.stt_lookahead import (  # noqa: E402
    apply_session_lookahead,
    lookahead_ms,
    parse_lookahead,
)

ENV_VAR = "STT_LOOKAHEAD_FRAMES"


def install_lookahead(server: Any, frames: int | None) -> None:
    """Patch the server's streaming-session opener.

    ``realtime_ws`` calls the module-level ``_open_streaming_session`` for the
    first turn and for every session it reopens after a commit, so wrapping
    that function covers the whole realtime path. The batch endpoint calls
    ``model.generate`` directly and is untouched.
    """
    if frames is None:
        return
    original = server._open_streaming_session

    def open_streaming_session(*args, **kwargs):
        return apply_session_lookahead(original(*args, **kwargs), frames)

    server._open_streaming_session = open_streaming_session


def install_from_environment(server: Any, environ: Mapping[str, str]) -> int | None:
    """Read the configured frame count, install it, and return it."""
    frames = parse_lookahead(environ.get(ENV_VAR))
    install_lookahead(server, frames)
    return frames


def main() -> None:
    import mlx_audio.server as server

    frames = install_from_environment(server, os.environ)
    if frames is None:
        print("STT lookahead: checkpoint default", flush=True)
    else:
        print(
            f"STT lookahead: {frames} frames ({lookahead_ms(frames)} ms)",
            flush=True,
        )
    server.main()


if __name__ == "__main__":
    main()
