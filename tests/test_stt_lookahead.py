"""The lookahead override is a realtime-session detail, so its rules are exact.

The served Nemotron checkpoint declares ``model_type: nemotron_asr`` and a
default attention context of ``[56, 13]``. mlx-audio's own transcription-delay
knob only reaches ``nemotron_asr_streaming`` models, so this module is the
supported path for the checkpoint we actually serve: it changes one streaming
session's right context only, only to a value the checkpoint declares.

The model itself is never touched. That is the property the batch endpoint and
the README's batch WER table depend on.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.stt_lookahead import (
    FRAME_MS,
    apply_session_lookahead,
    lookahead_ms,
    parse_lookahead,
)


def _pairs(supported):
    if supported is None:
        return None
    if len(supported) == 2 and all(isinstance(value, int) for value in supported):
        return [list(supported)]
    return [list(pair) for pair in supported]


def nemotron(
    *,
    model_type: str = "nemotron_asr",
    context: list[int] | None = (56, 13),
    supported: list[list[int]] | None = ((56, 13), (56, 6), (56, 3), (56, 0)),
):
    """A stand-in for the loaded model: only the fields the override reads."""
    return SimpleNamespace(
        model_type=model_type,
        default_att_context_size=None if context is None else list(context),
        encoder_config=SimpleNamespace(att_context_size=_pairs(supported)),
    )


def session(model=None, context=(56, 13)):
    """A stand-in for ``NemotronStreamingSession``: the fields it exposes."""
    live = SimpleNamespace(
        model=model if model is not None else nemotron(),
        att_context_size=None if context is None else list(context),
        resets=0,
    )
    live.reset = lambda: _count_reset(live)
    return live


def _count_reset(holder):
    holder.resets += 1


class TestParseLookahead:
    def test_unset_or_blank_keeps_the_checkpoint_default(self):
        assert parse_lookahead(None) is None
        assert parse_lookahead("") is None
        assert parse_lookahead("   ") is None

    @pytest.mark.parametrize("frames", [0, 3, 6, 13])
    def test_accepts_each_supported_frame_count(self, frames):
        assert parse_lookahead(frames) == frames
        assert parse_lookahead(str(frames)) == frames

    def test_strips_surrounding_whitespace(self):
        assert parse_lookahead(" 6 ") == 6

    def test_rejects_a_value_that_is_not_an_integer(self):
        with pytest.raises(ValueError, match="integer"):
            parse_lookahead("six")

    def test_rejects_a_frame_count_the_checkpoint_does_not_support(self):
        with pytest.raises(ValueError, match="unsupported lookahead 5"):
            parse_lookahead("5")


class TestApplySessionLookahead:
    def test_none_leaves_the_session_untouched(self):
        live = session()
        live.resets = 0

        assert apply_session_lookahead(live, None) is live
        assert live.att_context_size == [56, 13]
        assert live.resets == 0

    def test_changes_the_right_context_and_keeps_the_left(self):
        live = session()

        assert apply_session_lookahead(live, 6) is live
        assert live.att_context_size == [56, 6]

    def test_resets_the_session_so_the_new_context_builds_the_state(self):
        live = session()

        apply_session_lookahead(live, 6)

        assert live.resets == 1

    def test_accepts_the_checkpoints_zero_lookahead(self):
        live = session()

        apply_session_lookahead(live, 0)

        assert live.att_context_size == [56, 0]
        assert live.resets == 1

    def test_never_changes_the_model_default(self):
        model = nemotron()
        live = session(model=model)

        apply_session_lookahead(live, 6)

        assert live.att_context_size == [56, 6]
        assert model.default_att_context_size == [56, 13]

    def test_leaves_sessions_of_other_models_alone(self):
        # The same server hosts Kokoro TTS and other STT checkpoints.
        model = SimpleNamespace(model_type="kokoro", default_att_context_size=None)
        live = session(model=model, context=None)
        live.resets = 0

        assert apply_session_lookahead(live, 6) is live
        assert live.att_context_size is None
        assert live.resets == 0

    def test_leaves_a_session_without_a_model_alone(self):
        live = SimpleNamespace(att_context_size=[56, 13], resets=0, reset=lambda: None)

        assert apply_session_lookahead(live, 6) is live
        assert live.att_context_size == [56, 13]

    def test_also_handles_the_natively_streaming_variant(self):
        live = session(model=nemotron(model_type="nemotron_asr_streaming"))

        apply_session_lookahead(live, 3)

        assert live.att_context_size == [56, 3]

    def test_rejects_a_lookahead_this_checkpoint_does_not_declare(self):
        live = session(model=nemotron(supported=((56, 13),)))
        live.resets = 0

        with pytest.raises(ValueError, match=r"supported right contexts: \[13\]"):
            apply_session_lookahead(live, 6)

        assert live.att_context_size == [56, 13]
        assert live.resets == 0

    def test_rejects_a_nemotron_session_without_an_attention_context(self):
        live = session(context=None)

        with pytest.raises(ValueError, match="attention context"):
            apply_session_lookahead(live, 6)

    def test_accepts_a_checkpoint_that_declares_one_context_pair(self):
        live = session(model=nemotron(supported=(56, 6)))

        apply_session_lookahead(live, 6)

        assert live.att_context_size == [56, 6]

    def test_a_checkpoint_without_declared_contexts_is_trusted(self):
        live = session(model=nemotron(supported=None))

        apply_session_lookahead(live, 6)

        assert live.att_context_size == [56, 6]

    def test_applying_the_same_value_twice_is_idempotent(self):
        live = session()

        apply_session_lookahead(live, 6)
        apply_session_lookahead(live, 6)

        assert live.att_context_size == [56, 6]
        assert live.resets == 2


class TestLookaheadMilliseconds:
    def test_each_frame_is_eighty_milliseconds_of_audio(self):
        assert FRAME_MS == 80
        assert lookahead_ms(13) == 1040
        assert lookahead_ms(6) == 480
        assert lookahead_ms(3) == 240
        assert lookahead_ms(0) == 0
