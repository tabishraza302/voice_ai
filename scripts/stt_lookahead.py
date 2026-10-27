"""Apply a supported lookahead to one realtime Nemotron streaming session.

The checkpoint we serve (`mlx-community/nemotron-3.5-asr-streaming-0.6b`)
declares ``model_type: nemotron_asr``, not ``nemotron_asr_streaming``. That
matters: mlx-audio's ``--realtime-transcription-delay-ms`` knob only reaches
models whose ``model_type`` is the latter, so for this checkpoint the flag is
accepted and silently ignored. The checkpoint does declare a set of supported
far-lookahead values, and each streaming session reads the chosen one from its
own ``att_context_size``.

So this module changes one session, after the server opens it and before any
audio reaches it: the right context, only to a value the checkpoint declares.
The loaded model keeps its default, which is what the batch endpoint and the
README's batch WER table are measured against.
"""

from __future__ import annotations

from typing import Any

# The encoder runs at 12.5 frames/s after subsampling: one frame of right
# context is 80 ms of audio, and the native chunk is (right + 1) frames.
FRAME_MS = 80
SUPPORTED_LOOKAHEADS = (0, 3, 6, 13)
NEMOTRON_MODEL_TYPES = ("nemotron_asr", "nemotron_asr_streaming")


def parse_lookahead(raw: str | int | None) -> int | None:
    """Parse the configured value; blank means "keep the checkpoint default"."""
    if raw is None:
        return None
    if isinstance(raw, int):
        frames = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            frames = int(text)
        except ValueError as exc:
            raise ValueError(f"lookahead must be an integer, got {raw!r}") from exc
    if frames not in SUPPORTED_LOOKAHEADS:
        raise ValueError(
            f"unsupported lookahead {frames}; expected one of "
            f"{list(SUPPORTED_LOOKAHEADS)}"
        )
    return frames


def lookahead_ms(frames: int) -> int:
    """How much audio one frame of right context covers."""
    return frames * FRAME_MS


def _declared_right_contexts(encoder_config: Any) -> set[int]:
    """The right contexts the checkpoint declares, in either config shape."""
    raw = getattr(encoder_config, "att_context_size", None) or []
    if len(raw) == 2 and all(isinstance(value, int) for value in raw):
        pairs = [raw]
    else:
        pairs = raw
    return {int(pair[1]) for pair in pairs if len(pair) == 2}


def apply_session_lookahead(session: Any, frames: int | None) -> Any:
    """Set one streaming session's right context and rebuild its state.

    A session of any other model is returned untouched (the same server hosts
    Kokoro TTS and other checkpoints). A Nemotron whose checkpoint does not
    support the requested frame count is an error, not a silent fallback.

    The model is deliberately not mutated: ``session.reset()`` builds the
    encoder state from ``session.att_context_size``, so the override belongs to
    the session and the batch endpoint keeps the checkpoint default.
    """
    if frames is None:
        return session

    model = getattr(session, "model", None)
    if getattr(model, "model_type", None) not in NEMOTRON_MODEL_TYPES:
        return session

    context = getattr(session, "att_context_size", None)
    if not context or len(context) != 2:
        raise ValueError("Nemotron session has no [left, right] attention context")

    supported = _declared_right_contexts(getattr(model, "encoder_config", None))
    if supported and frames not in supported:
        raise ValueError(
            f"lookahead {frames} is not supported by this checkpoint "
            f"(supported right contexts: {sorted(supported)})"
        )

    session.att_context_size = [int(context[0]), frames]
    session.reset()
    return session
