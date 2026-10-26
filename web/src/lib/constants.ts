import type { ModelCatalog } from "@/types/agent";

export const DEFAULT_CATALOG: ModelCatalog = {
  llm_models: [
    {
      id: "qwen3.5:4b",
      name: "Qwen 3.5 4B (Local Ollama)",
      description: "Default local language model for voice sessions",
      supports_reasoning: true,
    },
    {
      id: "qwen2.5:3b",
      name: "Qwen 2.5 3B",
      description: "Smaller local language model",
      supports_reasoning: false,
    },
    {
      id: "llama3.2:3b",
      name: "Llama 3.2 3B",
      description: "Meta lightweight conversational model",
      supports_reasoning: false,
    },
  ],
  stt_providers: [
    {
      id: "mlx-audio-realtime",
      name: "Streaming Nemotron 3.5 (Metal GPU)",
      description: "Realtime WebSocket streaming ASR via mlx-audio on Apple Silicon GPU",
    },
    {
      id: "mlx-audio",
      name: "Batch Nemotron 3.5 (Metal GPU)",
      description: "Batch HTTP transcription with language prompt conditioning",
    },
    {
      id: "mlx-whisper",
      name: "Whisper Large v3 Turbo (Metal GPU)",
      description: "In-process MLX Whisper transcription",
    },
  ],
  tts_voices: [
    {
      id: "af_heart",
      name: "Heart (Female, EN)",
      language: "en",
      gender: "female",
      description: "Warm, natural conversational English female voice",
    },
    {
      id: "af_bella",
      name: "Bella (Female, EN)",
      language: "en",
      gender: "female",
      description: "Expressive, clear English female voice",
    },
    {
      id: "am_adam",
      name: "Adam (Male, EN)",
      language: "en",
      gender: "male",
      description: "Grounded, articulate English male voice",
    },
    {
      id: "am_michael",
      name: "Michael (Male, EN)",
      language: "en",
      gender: "male",
      description: "Professional, authoritative English male voice",
    },
    {
      id: "hf_alpha",
      name: "Alpha (Female, HI)",
      language: "hi",
      gender: "female",
      description: "Authentic Hindi female voice with natural prosody",
    },
    {
      id: "hf_beta",
      name: "Beta (Female, HI)",
      language: "hi",
      gender: "female",
      description: "Warm Hindi female voice for narrative guidance",
    },
    {
      id: "hm_omega",
      name: "Omega (Male, HI)",
      language: "hi",
      gender: "male",
      description: "Deep, confident Hindi male voice",
    },
    {
      id: "hm_psi",
      name: "Psi (Male, HI)",
      language: "hi",
      gender: "male",
      description: "Clear, friendly Hindi male voice",
    },
  ],
};

// Ranges mirror bot/agents/schemas.py (llm_temperature 0..2, llm_max_tokens
// 1..4096, tts_speed 0.5..2.0). Do not narrow them to the editor's convenience.
export const SPEED = { min: 0.5, max: 2.0, step: 0.05 } as const;
export const TEMPERATURE = { min: 0, max: 2.0, step: 0.05 } as const;
export const MAX_TOKENS = { min: 1, max: 4096, step: 1 } as const;
