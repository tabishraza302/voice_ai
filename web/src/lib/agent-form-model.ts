import type { Agent, AgentCreate, AgentUpdate } from "@/types/agent";

export interface AgentFormValues {
  name: string;
  slug: string;
  description: string;
  persona_prompt: string;
  llm_model: string;
  llm_temperature: number;
  llm_reasoning_effort: string;
  llm_max_tokens: number;
  stt_provider: string;
  tts_voice_en: string;
  tts_voice_hi: string;
  tts_speed: number;
}

export interface AgentFormErrors {
  name?: string;
  slug?: string;
  persona?: string;
}

const MAX_SLUG_LENGTH = 64;
const FALLBACK_SLUG = "voice-agent";

export const DEFAULT_NEW_AGENT_FORM: AgentFormValues = {
  name: "New Voice Agent",
  slug: "new-voice-agent",
  description: "",
  persona_prompt:
    "You are a friendly, knowledgeable, and quick voice assistant. Respond with brevity and natural speech rhythm.",
  llm_model: "qwen3.5:4b",
  llm_temperature: 0.6,
  llm_reasoning_effort: "none",
  llm_max_tokens: 256,
  stt_provider: "mlx-audio-realtime",
  tts_voice_en: "af_heart",
  tts_voice_hi: "hf_alpha",
  tts_speed: 1.0,
};

export function defaultAgentFormValues(catalog?: {
  llm_models?: Array<{ id: string }>;
  stt_providers?: Array<{ id: string }>;
  tts_voices?: Array<{ id: string; language: "en" | "hi" }>;
}): AgentFormValues {
  const firstModel = catalog?.llm_models?.[0]?.id;
  const firstStt = catalog?.stt_providers?.[0]?.id;
  const firstEnVoice = catalog?.tts_voices?.find((v) => v.language === "en")?.id;
  const firstHiVoice = catalog?.tts_voices?.find((v) => v.language === "hi")?.id;

  return {
    ...DEFAULT_NEW_AGENT_FORM,
    llm_model: firstModel ?? DEFAULT_NEW_AGENT_FORM.llm_model,
    stt_provider: firstStt ?? DEFAULT_NEW_AGENT_FORM.stt_provider,
    tts_voice_en: firstEnVoice ?? DEFAULT_NEW_AGENT_FORM.tts_voice_en,
    tts_voice_hi: firstHiVoice ?? DEFAULT_NEW_AGENT_FORM.tts_voice_hi,
  };
}

export function resolveModelOptions(
  selectedModel: string,
  catalogModels: Array<{ id: string; name: string }>
): Array<{ id: string; name: string; isUnavailable?: boolean }> {
  const inCatalog = catalogModels.some((m) => m.id === selectedModel);
  if (!selectedModel || inCatalog) {
    return catalogModels;
  }
  return [
    {
      id: selectedModel,
      name: `${selectedModel} (unavailable)`,
      isUnavailable: true,
    },
    ...catalogModels,
  ];
}
export function reconcileCatalogSelection({
  currentModel,
  initialModel,
  modelTouched,
  catalogModels,
}: {
  currentModel: string;
  initialModel: string;
  modelTouched: boolean;
  catalogModels?: Array<{ id: string }>;
}): string {
  // Once explicitly selected or touched by user, preserve the user choice.
  if (modelTouched) {
    return currentModel;
  }
  // If catalog is empty or includes current selection, keep it.
  if (!catalogModels?.length || catalogModels.some((m) => m.id === currentModel)) {
    return currentModel;
  }
  // If model is untouched and matches the initial default, update to first catalog model.
  if (currentModel === initialModel) {
    return catalogModels[0].id;
  }
  return currentModel;
}


export function slugify(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, MAX_SLUG_LENGTH)
      .replace(/-+$/g, "") || FALLBACK_SLUG
  );
}

export function formFromAgent(agent: Agent): AgentFormValues {
  return {
    name: agent.name,
    slug: agent.slug,
    description: agent.description ?? "",
    persona_prompt: agent.persona_prompt,
    llm_model: agent.llm_model,
    llm_temperature: agent.llm_temperature,
    llm_reasoning_effort: agent.llm_reasoning_effort ?? "",
    llm_max_tokens: agent.llm_max_tokens,
    stt_provider: agent.stt_provider,
    tts_voice_en: agent.tts_voice_en,
    tts_voice_hi: agent.tts_voice_hi,
    tts_speed: agent.tts_speed,
  };
}

export function toCreatePayload(values: AgentFormValues): AgentCreate {
  return {
    ...toSettingsPayload(values),
    slug: values.slug.trim(),
  };
}

export function toUpdatePayload(values: AgentFormValues): AgentUpdate {
  return toSettingsPayload(values);
}

export function isDirty(values: AgentFormValues, agent: Agent): boolean {
  const baseline = formFromAgent(agent);
  // Slug is excluded: it is derived on create and never sent on update.
  return !(
    values.name.trim() === baseline.name.trim() &&
    values.description.trim() === baseline.description.trim() &&
    values.persona_prompt.trim() === baseline.persona_prompt.trim() &&
    values.llm_model === baseline.llm_model &&
    sameNumber(values.llm_temperature, baseline.llm_temperature) &&
    values.llm_reasoning_effort === baseline.llm_reasoning_effort &&
    sameNumber(values.llm_max_tokens, baseline.llm_max_tokens) &&
    values.stt_provider === baseline.stt_provider &&
    values.tts_voice_en === baseline.tts_voice_en &&
    values.tts_voice_hi === baseline.tts_voice_hi &&
    sameNumber(values.tts_speed, baseline.tts_speed)
  );
}

export interface ValidateFormOptions {
  requireSlug?: boolean;
}

export function validateForm(
  values: AgentFormValues,
  { requireSlug = true }: ValidateFormOptions = {}
): AgentFormErrors {
  const errors: AgentFormErrors = {};
  if (!values.name.trim()) errors.name = "Name is required.";
  if (requireSlug) {
    const slug = values.slug.trim();
    if (!slug) {
      errors.slug = "Slug is required.";
    } else if (slugify(slug) !== slug) {
      errors.slug = "Use lowercase letters, numbers and hyphens only.";
    }
  }
  if (!values.persona_prompt.trim()) errors.persona = "Instructions are required.";
  return errors;
}

function toSettingsPayload(values: AgentFormValues) {
  return {
    name: values.name.trim(),
    description: values.description.trim(),
    persona_prompt: values.persona_prompt.trim(),
    llm_model: values.llm_model,
    llm_temperature: Number(values.llm_temperature),
    llm_reasoning_effort: values.llm_reasoning_effort,
    llm_max_tokens: Number(values.llm_max_tokens),
    stt_provider: values.stt_provider,
    tts_voice_en: values.tts_voice_en,
    tts_voice_hi: values.tts_voice_hi,
    tts_speed: Number(values.tts_speed),
  };
}

function sameNumber(current: number, baseline: number): boolean {
  const value = Number(current);
  return Number.isFinite(value) && value === Number(baseline);
}
