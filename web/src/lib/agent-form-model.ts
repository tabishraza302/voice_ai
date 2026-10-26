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

export function validateForm(values: AgentFormValues): AgentFormErrors {
  const errors: AgentFormErrors = {};
  if (!values.name.trim()) errors.name = "Name is required.";
  const slug = values.slug.trim();
  if (!slug || slugify(slug) !== slug) errors.slug = "Slug is required.";
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
