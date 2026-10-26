import { describe, expect, test } from "bun:test";
import {
  formFromAgent,
  isDirty,
  slugify,
  toCreatePayload,
  toUpdatePayload,
  validateForm,
} from "./agent-form-model.ts";

const agent = {
  id: "agent-1",
  slug: "bilingual-assistant",
  name: "Bilingual Assistant",
  description: "Speaks two languages.",
  persona_prompt: "Be helpful in English and Hindi.",
  llm_model: "qwen3.5:4b",
  llm_temperature: 0.7,
  llm_reasoning_effort: "",
  llm_max_tokens: 150,
  stt_provider: "mlx-audio",
  tts_voice_en: "af_heart",
  tts_voice_hi: "hf_alpha",
  tts_speed: 1,
  is_active: true,
  is_preset: false,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const form = {
  name: "Bilingual Assistant",
  slug: "bilingual-assistant",
  description: "Speaks two languages.",
  persona_prompt: "Be helpful in English and Hindi.",
  llm_model: "qwen3.5:4b",
  llm_temperature: 0.7,
  llm_reasoning_effort: "",
  llm_max_tokens: 150,
  stt_provider: "mlx-audio",
  tts_voice_en: "af_heart",
  tts_voice_hi: "hf_alpha",
  tts_speed: 1,
};

describe("slugify", () => {
  test("lowercases and collapses every non-alphanumeric run into one dash", () => {
    expect(slugify("Bilingual Assistant!")).toBe("bilingual-assistant");
    expect(slugify("a  --  b")).toBe("a-b");
    expect(slugify("Voice   Studio / Agents")).toBe("voice-studio-agents");
  });

  test("drops non-ASCII characters instead of transliterating them", () => {
    expect(slugify("  Héllo, Wörld  ")).toBe("h-llo-w-rld");
  });

  test("trims leading and trailing dashes", () => {
    expect(slugify("---leading and trailing---")).toBe("leading-and-trailing");
  });

  test("never returns an empty slug", () => {
    expect(slugify("")).toBe("voice-agent");
    expect(slugify("!!! ???")).toBe("voice-agent");
  });

  test("caps the slug at 64 characters and re-trims a trailing dash", () => {
    const slug = slugify(`${"a".repeat(63)} b`);

    expect(slug.length).toBeLessThanOrEqual(64);
    expect(slug.endsWith("-")).toBe(false);
    expect(slug).toBe("a".repeat(63));
  });
});

describe("formFromAgent", () => {
  test("maps every editable field from the agent response", () => {
    expect(formFromAgent(agent)).toEqual(form);
  });

  test("preserves an empty reasoning effort and a missing description", () => {
    const values = formFromAgent({ ...agent, description: undefined, llm_reasoning_effort: "" });

    expect(values.llm_reasoning_effort).toBe("");
    expect(values.description).toBe("");
  });
});

describe("payload builders", () => {
  test("toCreatePayload trims text, coerces numbers and sends exactly the editable fields", () => {
    const payload = toCreatePayload({
      name: "  Hindi Tutor  ",
      slug: "  hindi-tutor  ",
      description: "  Practice Hindi.  ",
      persona_prompt: "  Speak slowly.  ",
      llm_model: "qwen3.5:4b",
      llm_temperature: "0.35",
      llm_reasoning_effort: "low",
      llm_max_tokens: "512",
      stt_provider: "mlx-whisper",
      tts_voice_en: "af_bella",
      tts_voice_hi: "hf_beta",
      tts_speed: "1.25",
    });

    expect(payload).toEqual({
      slug: "hindi-tutor",
      name: "Hindi Tutor",
      description: "Practice Hindi.",
      persona_prompt: "Speak slowly.",
      llm_model: "qwen3.5:4b",
      llm_temperature: 0.35,
      llm_reasoning_effort: "low",
      llm_max_tokens: 512,
      stt_provider: "mlx-whisper",
      tts_voice_en: "af_bella",
      tts_voice_hi: "hf_beta",
      tts_speed: 1.25,
    });
    expect("is_active" in payload).toBe(false);
  });

  test("toCreatePayload keeps an empty description as an empty string", () => {
    const payload = toCreatePayload({ ...form, description: "   " });

    expect(payload.description).toBe("");
  });

  test("toUpdatePayload sends every editable field except slug and is_active", () => {
    const payload = toUpdatePayload({
      name: "  Hindi Tutor  ",
      slug: "  hindi-tutor  ",
      description: "  Practice Hindi.  ",
      persona_prompt: "  Speak slowly.  ",
      llm_model: "qwen2.5:3b",
      llm_temperature: "1.1",
      llm_reasoning_effort: "medium",
      llm_max_tokens: "900",
      stt_provider: "mlx-audio-realtime",
      tts_voice_en: "am_adam",
      tts_voice_hi: "hm_omega",
      tts_speed: "0.8",
    });

    expect(payload).toEqual({
      name: "Hindi Tutor",
      description: "Practice Hindi.",
      persona_prompt: "Speak slowly.",
      llm_model: "qwen2.5:3b",
      llm_temperature: 1.1,
      llm_reasoning_effort: "medium",
      llm_max_tokens: 900,
      stt_provider: "mlx-audio-realtime",
      tts_voice_en: "am_adam",
      tts_voice_hi: "hm_omega",
      tts_speed: 0.8,
    });
    expect("slug" in payload).toBe(false);
    expect("is_active" in payload).toBe(false);
  });
});

describe("isDirty", () => {
  test("is false for an unchanged copy of the agent", () => {
    expect(isDirty(formFromAgent(agent), agent)).toBe(false);
  });

  test("is true after any name, description, persona or settings change", () => {
    const changes = {
      name: "Renamed",
      description: "New description",
      persona_prompt: "New instructions",
      llm_model: "qwen2.5:3b",
      llm_temperature: 1.2,
      llm_reasoning_effort: "high",
      llm_max_tokens: 300,
      stt_provider: "mlx-whisper",
      tts_voice_en: "am_michael",
      tts_voice_hi: "hm_psi",
      tts_speed: 1.5,
    };

    for (const [field, value] of Object.entries(changes)) {
      expect(isDirty({ ...form, [field]: value }, agent)).toBe(true);
    }
  });

  test("ignores slug drift while editing an existing agent", () => {
    expect(isDirty({ ...form, slug: "a-different-slug" }, agent)).toBe(false);
  });

  test("ignores whitespace-only text edits because the payload trims them", () => {
    expect(isDirty({ ...form, name: `  ${form.name}  `, description: ` ${form.description} ` }, agent)).toBe(false);
  });

  test("compares numbers through Number() so input strings are not dirty", () => {
    expect(isDirty({ ...form, llm_temperature: "0.7", llm_max_tokens: "150", tts_speed: "1" }, agent)).toBe(false);
  });
});

describe("validateForm", () => {
  test("returns no errors for a complete form", () => {
    expect(validateForm(form)).toEqual({});
  });

  test("requires a name after trimming", () => {
    expect(validateForm({ ...form, name: "   " })).toEqual({ name: "Name is required." });
  });

  test("requires a slug", () => {
    expect(validateForm({ ...form, slug: "  " })).toEqual({ slug: "Slug is required." });
  });

  test("rejects a stored slug that slugify would change when slug is required", () => {
    expect(validateForm({ ...form, slug: "Legacy Agent" })).toEqual({
      slug: "Use lowercase letters, numbers and hyphens only.",
    });
  });

  test("skips slug validation when slug is not required", () => {
    for (const slug of ["Legacy Agent", "", "  ", "Bilingual Assistant!"]) {
      expect(validateForm({ ...form, slug }, { requireSlug: false })).toEqual({});
    }
  });

  test("still requires name and instructions when slug is not required", () => {
    expect(
      validateForm({ ...form, slug: "Legacy Agent", name: "  ", persona_prompt: "\n\t" }, { requireSlug: false })
    ).toEqual({
      name: "Name is required.",
      persona: "Instructions are required.",
    });
  });

  test("rejects a slug that slugify would change", () => {
    expect(validateForm({ ...form, slug: "Bilingual Assistant!" })).toEqual({
      slug: "Use lowercase letters, numbers and hyphens only.",
    });
  });

  test("requires instructions", () => {
    expect(validateForm({ ...form, persona_prompt: "\n\t" })).toEqual({ persona: "Instructions are required." });
  });

  test("reports every blank field at once", () => {
    expect(validateForm({ ...form, name: "", slug: "", persona_prompt: "" })).toEqual({
      name: "Name is required.",
      slug: "Slug is required.",
      persona: "Instructions are required.",
    });
  });
});
