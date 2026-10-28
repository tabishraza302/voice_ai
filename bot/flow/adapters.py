"""Adapter protocols and capability models per flow-contract.md section 8.

Provider-neutral protocols; no concrete model, Pipecat, or transport imports.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from bot.flow.schemas import (
    AdapterCapabilities,
    CompiledFlow,
    FlowCommand,
    FlowEvent,
    Reduction,
    SessionState,
    StateProposal,
)


@runtime_checkable
class ModelAdapterProtocol(Protocol):
    """Protocol for model adapters producing state proposals and speech events."""

    @property
    def capabilities(self) -> AdapterCapabilities: ...

    async def generate_turn(
        self,
        compiled: CompiledFlow,
        state: SessionState,
        user_text: str,
        turn_id: str,
        generation_id: str,
    ) -> list[FlowEvent]: ...


@runtime_checkable
class PlaybackAdapterProtocol(Protocol):
    """Protocol for trusted transport playback adapters."""

    @property
    def capabilities(self) -> AdapterCapabilities: ...

    async def enqueue_manifest(
        self,
        manifest_id: str,
        step_id: str,
        fragments: list[Any],
    ) -> None: ...

    async def cancel_playback(
        self,
        generation_id: str,
        cancellation_id: str,
    ) -> None: ...


@runtime_checkable
class FlowRunnerProtocol(Protocol):
    """Protocol for the runner that serializes reducer calls and persists state."""

    async def process_event(
        self,
        event: FlowEvent,
    ) -> Reduction: ...
