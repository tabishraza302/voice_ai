"""Tests for the local mlx-audio TTS service."""

from __future__ import annotations

import io
import wave

import httpx
import pytest

from bot.config import Settings
from bot.services import build_tts
from bot.tts_service import LocalMlxTTSService


def make_wav_bytes(
    sample_rate: int = 24000,
    channels: int = 1,
    sampwidth: int = 2,
    num_samples: int = 100,
) -> bytes:
    """Helper to generate in-memory WAV bytes for testing synthesis response parsing."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        raw_frames = (
            b"\x00\x00" * num_samples * channels
            if sampwidth == 2
            else b"\x00" * num_samples * channels
        )
        wf.writeframes(raw_frames)
    return buf.getvalue()


@pytest.fixture
def service() -> LocalMlxTTSService:
    settings = Settings(_env_file=None, tts_provider="mlx-audio")
    return build_tts(settings)


class TestClientLifecycle:
    async def test_client_is_reused_across_synthesize_calls(self, service: LocalMlxTTSService):
        wav_data = make_wav_bytes()

        class MockResponse:
            status_code = 200
            content = wav_data

        calls = 0

        async def fake_post(url: str, json: dict):
            nonlocal calls
            calls += 1
            return MockResponse()

        client = service._get_client()
        setattr(client, "post", fake_post)

        # Call synthesize twice
        pcm1, rate1 = await service._synthesize({"input": "Hello"})
        first_client = service._client

        pcm2, rate2 = await service._synthesize({"input": "World"})
        second_client = service._client

        assert calls == 2
        assert rate1 == 24000
        assert rate2 == 24000
        assert first_client is not None
        assert first_client is second_client
        assert not first_client.is_closed

    async def test_cleanup_closes_client(self, service: LocalMlxTTSService):
        client = service._get_client()
        assert not client.is_closed

        await service.cleanup()

        assert client.is_closed
        assert service._client is None

    async def test_client_recreated_if_closed(self, service: LocalMlxTTSService):
        client1 = service._get_client()
        await client1.aclose()
        assert client1.is_closed

        client2 = service._get_client()
        assert client2 is not None
        assert client2 is not client1
        assert not client2.is_closed

        await service.cleanup()

    async def test_custom_client_injection(self):
        custom_client = httpx.AsyncClient(timeout=10)
        svc = LocalMlxTTSService(
            model="kokoro",
            base_url="http://localhost:8020",
            client=custom_client,
        )
        assert svc._client is custom_client
        assert svc._get_client() is custom_client

        await svc.cleanup()
        assert custom_client.is_closed


class TestSynthesizeErrors:
    async def test_non_200_raises_runtime_error(self, service: LocalMlxTTSService):
        class MockErrorResponse:
            status_code = 500
            text = "Internal Server Error"

        async def fake_post(url: str, json: dict):
            return MockErrorResponse()

        client = service._get_client()
        setattr(client, "post", fake_post)

        with pytest.raises(RuntimeError, match="TTS server returned 500"):
            await service._synthesize({"input": "Hello"})

        await service.cleanup()

    async def test_invalid_sample_width_raises_runtime_error(self, service: LocalMlxTTSService):
        # 8-bit sample width (1 byte) instead of 16-bit (2 bytes)
        wav_data = make_wav_bytes(sampwidth=1)

        class MockResponse:
            status_code = 200
            content = wav_data

        async def fake_post(url: str, json: dict):
            return MockResponse()

        client = service._get_client()
        setattr(client, "post", fake_post)

        with pytest.raises(RuntimeError, match="expected 16-bit PCM"):
            await service._synthesize({"input": "Hello"})

        await service.cleanup()
