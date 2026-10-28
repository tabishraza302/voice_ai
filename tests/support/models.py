"""Fake model factories conforming to backend interfaces for orchestration tests.

Implements the contract specified in verification-plan.md Section 4.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
from typing import Any, AsyncGenerator, Callable
import pytest


class HTTPRefusedError(RuntimeError):
    """Raised when a fake model component detects external HTTP attempts."""


class FakeSTTService:
    def __init__(
        self,
        scripted_frames: list[Any] | None = None,
        delay_event: asyncio.Event | None = None,
        raise_boundary: str | None = None,
    ) -> None:
        self.scripted_frames = list(scripted_frames or [])
        self.delay_event = delay_event
        self.raise_boundary = raise_boundary
        self.pcm_chunks: list[bytes] = []
        self.is_closed = False

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Any, None]:
        if self._attempts_external_http():
            raise HTTPRefusedError("external HTTP forbidden in fake STT")
        if self.raise_boundary == "before_stt":
            raise RuntimeError("simulated error at before_stt boundary")

        self.pcm_chunks.append(audio)

        if self.delay_event is not None:
            await self.delay_event.wait()

        if self.raise_boundary == "after_stt":
            raise RuntimeError("simulated error at after_stt boundary")

        for frame in self.scripted_frames:
            yield frame

    def _attempts_external_http(self) -> bool:
        return False

    async def close(self) -> None:
        self.is_closed = True


class FakeLLMService:
    def __init__(
        self,
        scripted_responses: list[str] | None = None,
        delay_event: asyncio.Event | None = None,
        raise_boundary: str | None = None,
    ) -> None:
        self.scripted_responses = list(scripted_responses or [])
        self.delay_event = delay_event
        self.raise_boundary = raise_boundary
        self.recorded_hashes: list[str] = []
        self.is_closed = False

    async def run_llm(self, prompt: str, config: dict[str, Any] | None = None) -> AsyncGenerator[str, None]:
        if self._attempts_external_http():
            raise HTTPRefusedError("external HTTP forbidden in fake LLM")
        if self.raise_boundary == "before_llm":
            raise RuntimeError("simulated error at before_llm boundary")

        # Record immutable prompt/config hash
        hasher = hashlib.sha256(prompt.encode("utf-8"))
        if config:
            hasher.update(str(sorted(config.items())).encode("utf-8"))
        self.recorded_hashes.append(hasher.hexdigest())

        if self.delay_event is not None:
            await self.delay_event.wait()

        if self.raise_boundary == "after_llm":
            raise RuntimeError("simulated error at after_llm boundary")

        for resp in self.scripted_responses:
            yield resp

    def _attempts_external_http(self) -> bool:
        return False

    async def close(self) -> None:
        self.is_closed = True


class FakeTTSService:
    def __init__(
        self,
        delay_event: asyncio.Event | None = None,
        raise_boundary: str | None = None,
        produce_late_cancelled: bool = False,
    ) -> None:
        self.delay_event = delay_event
        self.raise_boundary = raise_boundary
        self.produce_late_cancelled = produce_late_cancelled
        self.recorded_texts: list[str] = []
        self.cancellations = 0
        self.is_closed = False

    async def run_tts(self, text: str) -> AsyncGenerator[bytes, None]:
        if self._attempts_external_http():
            raise HTTPRefusedError("external HTTP forbidden in fake TTS")
        if self.raise_boundary == "before_tts":
            raise RuntimeError("simulated error at before_tts boundary")

        self.recorded_texts.append(text)

        if self.delay_event is not None:
            await self.delay_event.wait()

        if self.raise_boundary == "after_tts":
            raise RuntimeError("simulated error at after_tts boundary")

        # Generate deterministic synthetic PCM audio (e.g. 16000 Hz 16-bit)
        yield b"\x00\x00" * 320

        if self.produce_late_cancelled and self.cancellations > 0:
            # Emit late fragment after cancellation
            yield b"\x01\x00" * 160

    def cancel(self) -> None:
        self.cancellations += 1

    def _attempts_external_http(self) -> bool:
        return False

    async def close(self) -> None:
        self.is_closed = True


@dataclass
class FakeModelsHarness:
    stt_instances: list[FakeSTTService] = field(default_factory=list)
    llm_instances: list[FakeLLMService] = field(default_factory=list)
    tts_instances: list[FakeTTSService] = field(default_factory=list)

    @property
    def creation_counts(self) -> dict[str, int]:
        return {
            "stt": len(self.stt_instances),
            "llm": len(self.llm_instances),
            "tts": len(self.tts_instances),
        }

    @property
    def close_counts(self) -> dict[str, int]:
        return {
            "stt": sum(1 for s in self.stt_instances if s.is_closed),
            "llm": sum(1 for s in self.llm_instances if s.is_closed),
            "tts": sum(1 for s in self.tts_instances if s.is_closed),
        }

    def create_stt(self, **kwargs: Any) -> FakeSTTService:
        service = FakeSTTService(**kwargs)
        self.stt_instances.append(service)
        return service

    def create_llm(self, **kwargs: Any) -> FakeLLMService:
        service = FakeLLMService(**kwargs)
        self.llm_instances.append(service)
        return service

    def create_tts(self, **kwargs: Any) -> FakeTTSService:
        service = FakeTTSService(**kwargs)
        self.tts_instances.append(service)
        return service


@pytest.fixture
def fake_models() -> FakeModelsHarness:
    return FakeModelsHarness()
