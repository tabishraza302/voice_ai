export interface Agent {
  id: string;
  slug: string;
  name: string;
  description?: string;
  persona_prompt: string;
  llm_model: string;
  llm_temperature: number;
  llm_reasoning_effort: string;
  llm_max_tokens: number;
  stt_provider: string;
  tts_voice_en: string;
  tts_voice_hi: string;
  tts_speed: number;
  is_active: boolean;
  is_preset: boolean;
  created_at: string;
  updated_at: string;
}

export interface AgentCreate {
  slug: string;
  name: string;
  description?: string;
  persona_prompt: string;
  llm_model?: string;
  llm_temperature?: number;
  llm_reasoning_effort?: string;
  llm_max_tokens?: number;
  stt_provider?: string;
  tts_voice_en?: string;
  tts_voice_hi?: string;
  tts_speed?: number;
  is_active?: boolean;
}

export interface AgentUpdate {
  slug?: string;
  name?: string;
  description?: string;
  persona_prompt?: string;
  llm_model?: string;
  llm_temperature?: number;
  llm_reasoning_effort?: string;
  llm_max_tokens?: number;
  stt_provider?: string;
  tts_voice_en?: string;
  tts_voice_hi?: string;
  tts_speed?: number;
  is_active?: boolean;
}

export interface BasePrompt {
  id: string;
  name?: string;
  content: string;
  prompt_text?: string;
  version: number;
  created_at: string;
  updated_at: string;
  revisions?: PromptRevision[];
}

export interface PromptRevision {
  id: string;
  base_prompt_id: string;
  content: string;
  prompt_text?: string;
  version: number;
  change_summary?: string;
  change_reason?: string;
  created_at: string;
}

export interface ModelCatalog {
  llm_models: {
    id: string;
    name: string;
    description: string;
    supports_reasoning: boolean;
  }[];
  stt_providers: {
    id: string;
    name: string;
    description: string;
  }[];
  tts_voices: {
    id: string;
    name: string;
    language: "en" | "hi";
    gender: "female" | "male";
    description: string;
    sample_url?: string;
  }[];
}

export interface VoiceSample {
  id: string;
  name: string;
  language: "en" | "hi";
  gender: "female" | "male";
  preview_text: string;
  audio_url?: string;
}

export interface PromptPreviewRequest {
  base_prompt?: string;
  persona_prompt: string;
  variables?: Record<string, string>;
}

export interface PromptPreviewResponse {
  compiled_prompt: string;
  token_estimate: number;
}
