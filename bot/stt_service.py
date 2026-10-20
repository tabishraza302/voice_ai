"""STT backend for the local `mlx-audio` server.

Pipecat's ``OpenAISTTService`` is almost right for the local
``mlx_audio.server`` — it speaks the same OpenAI transcription API — but
three things stand in the way. This subclass fixes all three, and each fix
exists for a concrete, verified reason:

1. **Auto-detect is blocked.** ``OpenAISTTService._transcribe`` opens with
   ``assert self._settings.language is not None``. Nemotron's headline
   feature is language-ID prompt conditioning across 40 locales, so
   ``language=None`` (auto) is the mode worth having — and the stock
   service turns it into an ``AssertionError``.

2. **The language code is a Whisper assumption.** Pipecat maps languages
   through ``language_to_whisper_language``, which is correct for Whisper
   and wrong for Nemotron. Nemotron's own prompt dictionary is the
   authority, and it accepts both ``"hi"`` and ``"hi-IN"`` (121 keys).

3. **The response format defaults to ndjson.** ``mlx_audio.server``
   defaults to ``response_format="ndjson"`` and returns
   ``application/x-ndjson``. The OpenAI client expects JSON, and the stock
   service only sets ``response_format`` when probability metrics are on.

The class also adds ``swap_model``, which changes the *resident* model on
the server. That matters because ``mlx_audio.server`` caches every model
it loads and never evicts it: serving two models at once holds both in
unified memory. A swap is therefore always load-then-unload.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from loguru import logger
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.whisper.base_stt import Transcription
from pipecat.transcriptions.language import Language
from pipecat.utils.types import assert_given

# Nemotron's prompt dictionary keys. Verified against the MLX checkpoint's
# config.json: 121 keys, with "hi-IN" -> 6, "hi" -> 6, "auto" -> 101.
NEMOTRON_LANGUAGE_MAP: dict[Language, str] = {
    Language.EN: "en-US",
    Language.HI: "hi-IN",
    Language.HI_IN: "hi-IN",
}

# The key that asks Nemotron to detect the language itself.
NEMOTRON_AUTO = "auto"


class LocalMlxSTTService(OpenAISTTService):
    """STT against a local `mlx_audio.server` running Nemotron 3.5 ASR."""

    # The local server needs no key, but the OpenAI client requires one.
    _requires_api_key = False

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        language: Language | None = Language.EN,
        **kwargs: Any,
    ) -> None:
        # `language=None` means auto-detect. It survives `apply_update`
        # because Pipecat's `is_given` treats None as explicitly provided
        # (only NOT_GIVEN is skipped) — which is what lets Nemotron's
        # language-ID conditioning be reached at all.
        super().__init__(
            api_key="ollama",  # placeholder: the local server ignores it
            base_url=base_url,
            settings=self.Settings(model=model, language=language),
            **kwargs,
        )

    # -- language ---------------------------------------------------------

    def language_to_service_language(self, language: Language) -> str | None:
        """Map a Pipecat language to a Nemotron prompt key.

        This is the framework's hook, used for STT metadata frames and
        when settings are written. The request path uses ``_transcribe``
        below, which needs the same mapping but also has to handle the
        auto-detect case.
        """
        if language is None:
            return NEMOTRON_AUTO
        if isinstance(language, Language):
            return NEMOTRON_LANGUAGE_MAP.get(language, language.value)
        return str(language)

    # -- transcription ----------------------------------------------------

    async def _transcribe(self, audio: bytes) -> Transcription:
        """Transcribe one VAD-delimited segment.

        Overrides the stock implementation to (a) allow auto-detect,
        (b) send Nemotron's own prompt key rather than a Whisper code, and
        (c) ask for JSON rather than the server's default ndjson.
        """
        model = assert_given(self._settings.model)

        # Pipecat stores the *service-native* language: it converts the
        # Language enum through language_to_service_language() when the
        # settings are written, so this is already "hi-IN" rather than
        # Language.HI. The enum case is handled anyway, in case a
        # settings object is assigned directly.
        requested = self._settings.language
        if requested is None:
            language = NEMOTRON_AUTO
        elif isinstance(requested, Language):
            language = NEMOTRON_LANGUAGE_MAP.get(requested, requested.value)
        else:
            language = str(requested)

        logger.debug(f"STT request: model={model} language={language}")

        return await self._client.audio.transcriptions.create(
            file=("audio.wav", audio, "audio/wav"),
            model=model,
            language=language,
            response_format="json",
        )

    # -- resident-model management ---------------------------------------

    async def swap_model(self, model_name: str) -> None:
        """Change which model the server holds resident.

        ``mlx_audio.server`` caches every model it loads and never evicts
        it, so the new model is loaded *before* the old one is dropped and
        the two are never both resident for long. This is the runtime half
        of the STT layer's swappability; the config half
        (``stt_provider`` / ``stt_model``) is the restart-time path.
        """
        root = self._client.base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")]

        current = self._settings.model
        if current == model_name:
            logger.debug(f"STT server already serving {model_name}")
            return

        # 1. Load the new model. The endpoint takes model_name as a query
        #    parameter, not a JSON body.
        try:
            query = urllib.parse.urlencode({"model_name": model_name})
            request = urllib.request.Request(f"{root}/v1/models?{query}", method="POST")
            with urllib.request.urlopen(request, timeout=900) as response:
                response.read()
            logger.info(f"STT server loaded {model_name}")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"failed to load STT model {model_name}: {exc}") from exc

        # 2. Drop the old one, so the two are never both resident.
        if current:
            try:
                query = urllib.parse.urlencode({"model_name": current})
                request = urllib.request.Request(
                    f"{root}/v1/models?{query}", method="DELETE"
                )
                with urllib.request.urlopen(request, timeout=120) as response:
                    response.read()
                logger.info(f"STT server released {current}")
            except urllib.error.HTTPError as exc:
                # 404 just means it was not resident.
                if exc.code != 404:
                    raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"could not release STT model {current}: {exc}")

        self._settings.model = model_name
