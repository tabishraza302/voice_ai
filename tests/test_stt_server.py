"""The wrapper is the only place the lookahead reaches a streaming session.

It patches ``mlx_audio.server._open_streaming_session`` — the seam every
``/v1/realtime`` session goes through — and nothing else. The loaded model and
the batch endpoint stay on the checkpoint default, so the README's batch WER
numbers still describe the code that is running.

These tests pin that contract without importing MLX.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.stt_server import ENV_VAR, install_from_environment, install_lookahead


def nemotron(context=(56, 13)):
    return SimpleNamespace(
        model_type="nemotron_asr",
        default_att_context_size=list(context),
        encoder_config=SimpleNamespace(
            att_context_size=[[56, 13], [56, 6], [56, 3], [56, 0]]
        ),
    )


def streaming_session(model, context=(56, 13)):
    live = SimpleNamespace(
        model=model,
        att_context_size=None if context is None else list(context),
        resets=0,
    )
    live.reset = lambda: _count_reset(live)
    return live


def _count_reset(holder):
    holder.resets += 1


class FakeServer:
    """The two module-level behaviours the wrapper relies on."""

    def __init__(self, model):
        self.model = model
        self.opened: list[dict] = []
        self._open_streaming_session = self._original

    def _original(self, model, *, temperature, delay_ms):
        self.opened.append(
            {"model": model, "temperature": temperature, "delay_ms": delay_ms}
        )
        context = getattr(model, "default_att_context_size", None)
        return streaming_session(model, context=context)


class TestInstallLookahead:
    def test_no_frame_count_leaves_the_session_opener_alone(self):
        server = FakeServer(nemotron())
        original = server._open_streaming_session

        install_lookahead(server, None)

        assert server._open_streaming_session == original

    def test_patched_opener_applies_the_lookahead_to_the_new_session(self):
        model = nemotron()
        server = FakeServer(model)

        install_lookahead(server, 6)
        live = server._open_streaming_session(model, temperature=0.0, delay_ms=None)

        assert live.att_context_size == [56, 6]
        assert server.opened == [
            {"model": model, "temperature": 0.0, "delay_ms": None}
        ]

    def test_patched_opener_passes_the_servers_arguments_through(self):
        model = nemotron()
        server = FakeServer(model)
        install_lookahead(server, 3)

        server._open_streaming_session(model, temperature=0.0, delay_ms=560)

        assert server.opened == [
            {"model": model, "temperature": 0.0, "delay_ms": 560}
        ]

    def test_patched_opener_leaves_the_model_on_its_checkpoint_default(self):
        model = nemotron()
        server = FakeServer(model)
        install_lookahead(server, 6)

        server._open_streaming_session(model, temperature=0.0, delay_ms=None)

        # Batch transcription reads this, so it must not move.
        assert model.default_att_context_size == [56, 13]

    def test_each_session_gets_the_override_from_the_same_model(self):
        model = nemotron()
        server = FakeServer(model)
        install_lookahead(server, 6)

        first = server._open_streaming_session(model, temperature=0.0, delay_ms=None)
        second = server._open_streaming_session(model, temperature=0.0, delay_ms=None)

        assert first is not second
        assert first.att_context_size == [56, 6]
        assert second.att_context_size == [56, 6]
        assert model.default_att_context_size == [56, 13]

    def test_patched_opener_leaves_a_non_nemotron_session_alone(self):
        kokoro = SimpleNamespace(model_type="kokoro", default_att_context_size=None)
        server = FakeServer(kokoro)
        install_lookahead(server, 6)

        live = server._open_streaming_session(kokoro, temperature=0.0, delay_ms=None)

        assert live.att_context_size is None  # the fake's untouched default
        assert live.resets == 0  # the override did not reset it

    def test_does_not_touch_a_model_provider_loader(self):
        server = FakeServer(nemotron())
        server.model_provider = SimpleNamespace(load_model=lambda name: name)

        install_lookahead(server, 6)

        assert server.model_provider.load_model("anything") == "anything"


class TestInstallFromEnvironment:
    def test_reads_the_frame_count_from_the_environment(self):
        model = nemotron()
        server = FakeServer(model)

        assert install_from_environment(server, {ENV_VAR: "6"}) == 6
        live = server._open_streaming_session(model, temperature=0.0, delay_ms=None)

        assert live.att_context_size == [56, 6]

    def test_an_absent_or_blank_value_keeps_the_checkpoint_default(self):
        model = nemotron()
        server = FakeServer(model)

        assert install_from_environment(server, {}) is None
        assert install_from_environment(server, {ENV_VAR: "  "}) is None
        live = server._open_streaming_session(model, temperature=0.0, delay_ms=None)

        assert live.att_context_size == [56, 13]

    def test_an_invalid_value_fails_before_the_server_starts(self):
        server = FakeServer(nemotron())

        with pytest.raises(ValueError, match="unsupported lookahead 5"):
            install_from_environment(server, {ENV_VAR: "5"})
