from __future__ import annotations

from typing import Any
from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class _MockProviderService(FrameProcessor):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._handlers: dict[str, Any] = {}

    def register_function(self, name: str, callback: Any) -> None:
        pass

    def add_event_handler(self, event: str, handler: Any) -> None:
        self._handlers[event] = handler

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


def _patch_providers() -> None:
    import app.voice as voice_module

    voice_module.DeepgramSTTService = _MockProviderService  # type: ignore[misc]
    voice_module.AzureSTTService = _MockProviderService  # type: ignore[misc]
    voice_module.OpenAILLMService = _MockProviderService  # type: ignore[misc]
    voice_module.CartesiaTTSService = _MockProviderService  # type: ignore[misc]
    voice_module.ElevenLabsTTSService = _MockProviderService  # type: ignore[misc]
    voice_module.CachedCartesiaTTSService = _MockProviderService  # type: ignore[misc]
    voice_module.CachedElevenLabsTTSService = _MockProviderService  # type: ignore[misc]


def __getattr__(name: str) -> Any:
    """Lazy loader for uvicorn entrypoint `tests.test_worker:app`.

    Prevents import-time provider monkeypatching during pytest test collection
    in the parent process. Only runs when `app` is explicitly accessed.
    """
    if name == "app":
        _patch_providers()
        from app.main import app as main_app
        return main_app
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

