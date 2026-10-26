from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SUPPORTED_STT_PROVIDERS = {"mlx-audio", "mlx-audio-realtime", "mlx-whisper"}
SUPPORTED_REASONING_EFFORTS = {"none", "low", "medium", "high"}
SUPPORTED_TTS_VOICES_EN = {"af_heart", "af_bella", "am_adam", "am_michael"}
SUPPORTED_TTS_VOICES_HI = {"hf_alpha", "hf_beta", "hm_omega", "hm_psi"}


class AgentBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field("", max_length=4096)
    persona_prompt: str = Field("", max_length=20000)
    llm_model: str = Field("qwen3.5:4b", min_length=1, max_length=64)
    llm_temperature: float = Field(0.7, ge=0.0, le=2.0)
    llm_reasoning_effort: str = Field("none", max_length=16)
    llm_max_tokens: int = Field(150, ge=1, le=4096)
    stt_provider: str = Field("mlx-audio", min_length=1, max_length=32)
    tts_voice_en: str = Field("af_heart", min_length=1, max_length=32)
    tts_voice_hi: str = Field("hf_alpha", min_length=1, max_length=32)
    tts_speed: float = Field(1.0, ge=0.5, le=2.0)

    @field_validator("name")
    @classmethod
    def name_must_contain_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value

    @field_validator("llm_model")
    @classmethod
    def model_must_contain_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("llm_model must not be blank")
        return value

    @field_validator("llm_reasoning_effort")
    @classmethod
    def reasoning_effort_must_be_supported(cls, value: str) -> str:
        if value and value not in SUPPORTED_REASONING_EFFORTS:
            raise ValueError(f"llm_reasoning_effort must be one of {sorted(SUPPORTED_REASONING_EFFORTS)}")
        if value.strip() != value:
            raise ValueError("llm_reasoning_effort must not contain surrounding whitespace")
        return value

    @field_validator("stt_provider")
    @classmethod
    def stt_provider_must_be_supported(cls, value: str) -> str:
        if value not in SUPPORTED_STT_PROVIDERS:
            raise ValueError(f"unsupported stt_provider: {value}")
        return value

    @field_validator("tts_voice_en")
    @classmethod
    def english_voice_must_be_supported(cls, value: str) -> str:
        if value not in SUPPORTED_TTS_VOICES_EN:
            raise ValueError(f"unsupported English TTS voice: {value}")
        return value

    @field_validator("tts_voice_hi")
    @classmethod
    def hindi_voice_must_be_supported(cls, value: str) -> str:
        if value not in SUPPORTED_TTS_VOICES_HI:
            raise ValueError(f"unsupported Hindi TTS voice: {value}")
        return value


class AgentCreate(AgentBase):
    slug: str = Field(..., min_length=1, max_length=64)

    @field_validator("slug")
    @classmethod
    def slug_must_contain_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("slug must not be blank")
        return value


class AgentUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = Field(None, max_length=4096)
    persona_prompt: str | None = Field(None, max_length=20000)
    llm_model: str | None = Field(None, min_length=1, max_length=64)
    llm_temperature: float | None = Field(None, ge=0.0, le=2.0)
    llm_reasoning_effort: str | None = Field(None, max_length=16)
    llm_max_tokens: int | None = Field(None, ge=1, le=4096)
    stt_provider: str | None = Field(None, min_length=1, max_length=32)
    tts_voice_en: str | None = Field(None, min_length=1, max_length=32)
    tts_voice_hi: str | None = Field(None, min_length=1, max_length=32)
    tts_speed: float | None = Field(None, ge=0.5, le=2.0)

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        null_fields = [name for name in self.model_fields_set if getattr(self, name) is None]
        if null_fields:
            raise ValueError(f"fields cannot be null: {', '.join(sorted(null_fields))}")
        return self

    @field_validator("name")
    @classmethod
    def name_must_contain_non_whitespace(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("name must not be blank")
        return value

    @field_validator("llm_model")
    @classmethod
    def model_must_contain_non_whitespace(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("llm_model must not be blank")
        return value

    @field_validator("llm_reasoning_effort")
    @classmethod
    def reasoning_effort_must_be_supported(cls, value: str | None) -> str | None:
        if value is not None and value and value not in SUPPORTED_REASONING_EFFORTS:
            raise ValueError(f"llm_reasoning_effort must be one of {sorted(SUPPORTED_REASONING_EFFORTS)}")
        if value is not None and value.strip() != value:
            raise ValueError("llm_reasoning_effort must not contain surrounding whitespace")
        return value

    @field_validator("stt_provider")
    @classmethod
    def stt_provider_must_be_supported(cls, value: str | None) -> str | None:
        if value is not None and value not in SUPPORTED_STT_PROVIDERS:
            raise ValueError(f"unsupported stt_provider: {value}")
        return value

    @field_validator("tts_voice_en")
    @classmethod
    def english_voice_must_be_supported(cls, value: str | None) -> str | None:
        if value is not None and value not in SUPPORTED_TTS_VOICES_EN:
            raise ValueError(f"unsupported English TTS voice: {value}")
        return value

    @field_validator("tts_voice_hi")
    @classmethod
    def hindi_voice_must_be_supported(cls, value: str | None) -> str | None:
        if value is not None and value not in SUPPORTED_TTS_VOICES_HI:
            raise ValueError(f"unsupported Hindi TTS voice: {value}")
        return value


class AgentResponse(AgentBase):
    id: str
    slug: str
    is_active: bool
    is_preset: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BasePromptUpdate(BaseModel):
    prompt_text: str | None = Field(None, max_length=20000)
    content: str | None = Field(None, max_length=20000)
    change_reason: str = Field("", max_length=255)
    change_summary: str = Field("", max_length=255)

    @model_validator(mode="after")
    def requires_prompt_text(self):
        if self.prompt_text is None and self.content is None:
            raise ValueError("prompt_text or content is required")
        return self

    def get_text(self) -> str:
        return self.prompt_text if self.prompt_text is not None else self.content or ""

    def get_reason(self) -> str:
        return self.change_reason or self.change_summary or ""


class PromptRevisionResponse(BaseModel):
    id: str
    base_prompt_id: str
    version: int
    prompt_text: str
    content: str = ""
    change_reason: str = ""
    change_summary: str = ""
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BasePromptResponse(BaseModel):
    id: str
    name: str
    prompt_text: str
    content: str = ""
    version: int
    created_at: datetime
    updated_at: datetime
    revisions: list[PromptRevisionResponse] = []

    model_config = ConfigDict(from_attributes=True)

class PromptPreviewRequest(BaseModel):
    base_prompt: str | None = Field(None, max_length=20000)
    persona_prompt: str | None = Field(None, max_length=20000)
    agent_id: str | None = None
    variables: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("variables")
    @classmethod
    def bound_variable_sizes(cls, values: dict[str, str]) -> dict[str, str]:
        if any(len(key) > 64 or len(value) > 2048 for key, value in values.items()):
            raise ValueError("prompt variables are limited to 64-character names and 2048-character values")
        return values


class PromptPreviewResponse(BaseModel):
    compiled_prompt: str
    token_estimate: int = 0
