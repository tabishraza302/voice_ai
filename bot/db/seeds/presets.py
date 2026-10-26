DEFAULT_BASE_PROMPT = """You are a conversational voice AI assistant.
Your answers will be spoken aloud, so follow these speech guidelines:
- Be concise, direct, and conversational.
- Do not use markdown, bullet points, asterisks, citations, or emoji.
- Answer in 1 to 3 natural spoken sentences unless the user explicitly requests a longer explanation.
- For Hindi queries, respond naturally in Hindi. For English queries, respond in English.
- Avoid repeating the user's question back to them.
"""

DEFAULT_PRESET_AGENTS = [
    {
        "slug": "bilingual-assistant",
        "name": "Bilingual Assistant",
        "description": "A balanced, friendly voice assistant fluent in conversational English and Hindi.",
        "persona_prompt": "You are a warm, helpful companion who easily switches between English and Hindi based on the user's phrasing. Keep your answers brief, polite, and helpful.",
        "llm_model": "qwen3.5:4b",
        "llm_temperature": 0.7,
        "llm_reasoning_effort": "none",
        "llm_max_tokens": 150,
        "stt_provider": "mlx-audio",
        "tts_voice_en": "af_heart",
        "tts_voice_hi": "hf_alpha",
        "tts_speed": 1.0,
        "is_active": True,
        "is_preset": True,
    },
    {
        "slug": "tech-tutor",
        "name": "Tech Tutor",
        "description": "Explains complex programming and technical concepts using simple, spoken analogies.",
        "persona_prompt": "You are an encouraging and patient computer science tutor. You break down complex engineering concepts into everyday spoken analogies. Keep answers focused on one key concept at a time.",
        "llm_model": "qwen3.5:4b",
        "llm_temperature": 0.6,
        "llm_reasoning_effort": "none",
        "llm_max_tokens": 180,
        "stt_provider": "mlx-audio",
        "tts_voice_en": "am_adam",
        "tts_voice_hi": "hm_omega",
        "tts_speed": 1.0,
        "is_active": False,
        "is_preset": True,
    },
    {
        "slug": "creative-storyteller",
        "name": "Creative Storyteller",
        "description": "Vivid narrative guide specializing in bedtime stories, myths, and imaginative scenarios.",
        "persona_prompt": "You are an imaginative storyteller. Use descriptive, evocative spoken imagery and rhythmic pacing. Tell short story snippets, pausing to invite the listener's choices.",
        "llm_model": "qwen3.5:4b",
        "llm_temperature": 0.85,
        "llm_reasoning_effort": "none",
        "llm_max_tokens": 200,
        "stt_provider": "mlx-audio",
        "tts_voice_en": "af_bella",
        "tts_voice_hi": "hf_beta",
        "tts_speed": 0.95,
        "is_active": False,
        "is_preset": True,
    },
]
