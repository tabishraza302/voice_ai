"use client";

import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  Save,
  AlertCircle,
  RefreshCw,
  Mic,
} from "lucide-react";
import { api } from "@/lib/api-client";
import {
  DEFAULT_CATALOG,
  MAX_TOKENS,
  SPEED,
  TEMPERATURE,
} from "@/lib/constants";
import {
  defaultAgentFormValues,
  formFromAgent,
  isDirty,
  reconcileCatalogSelection,
  resolveModelOptions,
  slugify,
  toCreatePayload,
  toUpdatePayload,
  validateForm,
  type AgentFormValues,
} from "@/lib/agent-form-model";
import { VoiceTestModal } from "@/components/VoiceTestModal";
import type { Agent } from "@/types/agent";

interface AgentFormProps {
  initialAgent?: Agent;
  isNew?: boolean;
}

export function AgentForm({ initialAgent, isNew = false }: AgentFormProps) {
  const router = useRouter();
  const [saving, setSaving] = useState(false);
  const [activating, setActivating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [catalog, setCatalog] = useState(DEFAULT_CATALOG);
  const [voiceModalOpen, setVoiceModalOpen] = useState(false);
  const [currentAgent, setCurrentAgent] = useState<Agent | undefined>(
    initialAgent
  );
  const initialFormValues = useMemo(
    () =>
      initialAgent
        ? formFromAgent(initialAgent)
        : defaultAgentFormValues(DEFAULT_CATALOG),
    [initialAgent]
  );
  const [formData, setFormData] = useState<AgentFormValues>(initialFormValues);
  const [slugTouched, setSlugTouched] = useState(!isNew);
  const modelTouchedRef = useRef(!isNew);

  useEffect(() => {
    async function loadCatalog() {
      try {
        const remote = await api.getModelCatalog();
        if (remote) {
          setCatalog(remote);
          if (isNew && remote.llm_models?.length) {
            setFormData((prev) => {
              const reconciled = reconcileCatalogSelection({
                currentModel: prev.llm_model,
                initialModel: initialFormValues.llm_model,
                modelTouched: modelTouchedRef.current,
                catalogModels: remote.llm_models,
              });
              if (reconciled === prev.llm_model) {
                return prev;
              }
              return {
                ...prev,
                llm_model: reconciled,
              };
            });
          }
        }
      } catch {
        // Keep default catalog
      }
    }
    loadCatalog();
  }, [isNew, initialFormValues.llm_model]);


  const estimatedTokens = useMemo(() => {
    return Math.ceil((formData.persona_prompt || "").length / 4);
  }, [formData.persona_prompt]);

  const activeAgentForTesting = currentAgent || null;
  const hasUnsavedChanges = Boolean(
    currentAgent && isDirty(formData, currentAgent)
  );

  const handleActivated = useCallback((activatedAgent: Agent) => {
    setCurrentAgent(activatedAgent);
  }, []);

  const handleSelectForNewSessions = async () => {
    if (!currentAgent || currentAgent.is_active || activating) return;
    if (hasUnsavedChanges) {
      setError("Save your changes before selecting this agent for new sessions.");
      return;
    }
    setActivating(true);
    setError(null);
    try {
      setCurrentAgent(await api.activateAgent(currentAgent.id));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not select this agent.");
    } finally {
      setActivating(false);
    }
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const errors = validateForm(formData, { requireSlug: isNew });
    const firstError = errors.name ?? errors.slug ?? errors.persona;
    if (firstError) {
      setError(firstError);
      return;
    }
    setSaving(true);
    try {
      if (isNew) {
        const created = await api.createAgent(toCreatePayload(formData));
        router.push(`/agents/${created.id}`);
      } else if (currentAgent) {
        const updated = await api.updateAgent(
          currentAgent.id,
          toUpdatePayload(formData)
        );
        setCurrentAgent(updated);
        setFormData(formFromAgent(updated));
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to save agent";
      setError(msg);
    } finally {
      setSaving(false);
    }
  };

  const handleLaunchVoiceTest = () => {
    if (isNew) {
      setError("Please save the new agent before launching live voice test.");
      return;
    }
    if (hasUnsavedChanges) {
      setError("Save your changes before testing. The voice session uses the saved agent configuration.");
      return;
    }
    setVoiceModalOpen(true);
  };

  return (
    <div className="space-y-6">
      {/* Editor heading and primary actions */}
      <div className="flex flex-col gap-5 border-b border-ink-800 pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div className="flex items-center gap-3">
          <Link
            href="/agents"
            className="button-secondary min-h-10 px-3"
            title="Back to agents list"
          >
            <ArrowLeft className="w-4 h-4" />
          </Link>
          <div>
            <div className="flex items-center gap-2.5 flex-wrap">
              <h1 className="text-2xl font-semibold tracking-tight text-limestone-100 flex items-center gap-2">
                {isNew ? "Create Agent" : formData.name}
              </h1>

              {currentAgent?.is_active ? (
                <span className="inline-flex items-center px-2 py-1 rounded-md text-xs font-medium bg-moss-950 text-moss-200 border border-moss-700">
                  Selected for new sessions
                </span>
              ) : (
                !isNew && <button type="button" onClick={() => void handleSelectForNewSessions()} disabled={activating} className="button-secondary min-h-8 px-2.5 py-1 text-xs">
                  {activating ? "Selecting…" : "Select for new sessions"}
                </button>
              )}

              {currentAgent?.is_preset && (
                <span className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium bg-copper-500/10 text-copper-300 border border-copper-500/20">
                  System preset
                </span>
              )}
            </div>
            <p className="text-sm text-limestone-400 mt-1">
              Configure identity, model behavior, and English and Hindi voices.
            </p>
          </div>
        </div>

        {/* Action Buttons */}
        <div className="flex flex-wrap items-center gap-2">
          <Link
            href="/agents"
            className="button-secondary"
          >
            Cancel
          </Link>

          {/* Dedicated Talk with Agent Primary CTA */}
          <button
            type="button"
            onClick={handleLaunchVoiceTest}
            className="button-secondary"
          >
            <Mic className="w-4 h-4 text-moss-100" />
            Talk with Agent
          </button>

          {/* Save Changes CTA */}
          <button
            type="submit"
            form="agent-editor"
            disabled={saving}
            className="button-primary"
          >
            {saving ? (
              <RefreshCw className="w-4 h-4 animate-spin" />
            ) : (
              <Save className="w-4 h-4" />
            )}
            {saving ? "Saving..." : isNew ? "Create Agent" : "Save Changes"}
          </button>
        </div>
      </div>

      {error && (
        <div role="alert" className="surface p-4 border-rust-800 bg-rust-950/40 text-rust-200 text-sm flex items-center gap-2">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Focused, single-column editor */}
      <form id="agent-editor" className="max-w-4xl" onSubmit={handleSave}>
        <div className="space-y-5">
          {/* Identity and shared persona */}
          <section className="border-b border-ink-800 py-6 first:pt-0">
            <div className="mb-5 flex flex-wrap items-center justify-between gap-4">
              <h2 className="text-lg font-semibold text-limestone-100">Identity and instructions</h2>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label htmlFor="agent-name" className="field-label">
                  Agent Name
                </label>
                <input
                  id="agent-name"
                  type="text"
                  value={formData.name}
                  onChange={(e) =>
                    setFormData({
                      ...formData,
                      name: e.target.value,
                      ...(isNew && !slugTouched
                        ? { slug: slugify(e.target.value) }
                        : {}),
                    })
                  }
                  required
                  className="field-control"
                  placeholder="e.g. Bilingual Assistant"
                />
              </div>
              <div>
                <label htmlFor="agent-slug" className="field-label">
                  Identifier Slug
                </label>
                <input
                  id="agent-slug"
                  type="text"
                  value={formData.slug}
                  onChange={(e) => {
                    setSlugTouched(true);
                    setFormData({ ...formData, slug: e.target.value });
                  }}
                  required
                  maxLength={64}
                  readOnly={!isNew}
                  className="field-control font-mono"
                  placeholder="e.g. bilingual-assistant"
                />
                {!isNew && <p className="mt-1.5 text-xs text-limestone-500">Agent identifiers stay fixed after creation.</p>}
              </div>
            </div>

            <div className="mt-5">
              <label htmlFor="agent-description" className="field-label">
                Short Description
              </label>
              <input
                id="agent-description"
                type="text"
                value={formData.description || ""}
                onChange={(e) =>
                  setFormData({ ...formData, description: e.target.value })
                }
                className="field-control"
                placeholder="e.g. Seamless Hinglish companion for everyday support"
              />
            </div>

            {/* Persona Prompt Editor */}
            <div className="mt-5">
              <div className="flex items-center justify-between mb-1.5">
                <label htmlFor="agent-persona" className="field-label">
                  Persona Prompt
                </label>
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-limestone-400 font-mono">
                    {formData.persona_prompt.length} chars
                  </span>
                  <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-copper-500/10 text-copper-300 border border-copper-500/20 font-semibold">
                    ~{estimatedTokens} tokens (est.)
                  </span>
                </div>
              </div>
              <textarea
                id="agent-persona"
                rows={7}
                value={formData.persona_prompt}
                onChange={(e) =>
                  setFormData({ ...formData, persona_prompt: e.target.value })
                }
                required
                maxLength={20000}
                className="field-control min-h-40 font-mono leading-relaxed"
                placeholder="Define character, tone of voice, brevity rules, or bilingual personality..."
              />
              <p className="mt-3 text-sm leading-6 text-limestone-400">
                Shared instructions apply to every agent. This prompt adds this agent’s role and tone.
              </p>
            </div>
          </section>

          {/* Voice configuration */}
          <section className="border-b border-ink-800 py-6">
            <h2 className="mb-5 text-lg font-semibold text-limestone-100">Voice selection</h2>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* English Voice Picker */}
              <fieldset className="min-w-0">
                <legend className="mb-1 text-sm font-medium text-limestone-200">English voice</legend>
                <p className="mb-3 text-xs text-limestone-500">Selected: {formData.tts_voice_en}</p>
                <div className="space-y-2">
                  {catalog.tts_voices
                    .filter((v) => v.language === "en")
                    .map((voice) => {
                      const isSelected = formData.tts_voice_en === voice.id;
                      return (
                        <label
                          key={voice.id}
                          className={`flex w-full items-center justify-between p-3 text-left rounded-lg cursor-pointer border transition-colors ${
                            isSelected
                              ? "bg-copper-500/10 border-copper-500/50 text-limestone-100"
                              : "bg-ink-900/50 border-ink-800/80 text-limestone-400 hover:border-ink-700"
                          }`}
                        >
                          <div>
                            <div className="flex items-center gap-2">
                              <span className="text-xs font-bold text-limestone-200">
                                {voice.name}
                              </span>
                              <span className="text-[10px] font-mono uppercase px-1.5 py-0.5 rounded bg-ink-800 text-limestone-400">
                                {voice.gender}
                              </span>
                            </div>
                            <p className="text-[11px] text-limestone-500 mt-0.5">
                              {voice.description}
                            </p>
                          </div>
                          <input
                            type="radio"
                            name="english-voice"
                            value={voice.id}
                            checked={isSelected}
                            onChange={() => setFormData({ ...formData, tts_voice_en: voice.id })}
                            className="ml-3 h-4 w-4 shrink-0 accent-copper-500"
                          />
                        </label>
                      );
                    })}
                </div>
              </fieldset>

              {/* Hindi Voice Picker */}
              <fieldset className="min-w-0">
                <legend className="mb-1 text-sm font-medium text-limestone-200">Hindi voice</legend>
                <p className="mb-3 text-xs text-limestone-500">Selected: {formData.tts_voice_hi}</p>
                <div className="space-y-2">
                  {catalog.tts_voices
                    .filter((v) => v.language === "hi")
                    .map((voice) => {
                      const isSelected = formData.tts_voice_hi === voice.id;
                      return (
                        <label
                          key={voice.id}
                          className={`flex w-full items-center justify-between p-3 text-left rounded-lg cursor-pointer border transition-colors ${
                            isSelected
                              ? "bg-moss-500/10 border-moss-500/50 text-limestone-100"
                              : "bg-ink-900/50 border-ink-800/80 text-limestone-400 hover:border-ink-700"
                          }`}
                        >
                          <div>
                            <div className="flex items-center gap-2">
                              <span className="text-xs font-bold text-limestone-200">
                                {voice.name}
                              </span>
                              <span className="text-[10px] font-mono uppercase px-1.5 py-0.5 rounded bg-ink-800 text-limestone-400">
                                {voice.gender}
                              </span>
                            </div>
                            <p className="text-[11px] text-limestone-500 mt-0.5">
                              {voice.description}
                            </p>
                          </div>
                          <input
                            type="radio"
                            name="hindi-voice"
                            value={voice.id}
                            checked={isSelected}
                            onChange={() => setFormData({ ...formData, tts_voice_hi: voice.id })}
                            className="ml-3 h-4 w-4 shrink-0 accent-moss-500"
                          />
                        </label>
                      );
                    })}
                </div>
              </fieldset>
            </div>

            {/* Speech Speed Slider */}
            <div className="pt-2">
              <div className="flex justify-between items-center mb-2">
                <label htmlFor="agent-tts-speed" className="text-sm font-medium text-limestone-300">
                  Synthesis Speed Multiplier:{" "}
                  <span className="text-moss-400 font-mono font-bold">
                    {formData.tts_speed.toFixed(2)}x
                  </span>
                </label>
                <span className="text-[11px] text-limestone-500">
                  {SPEED.min.toFixed(2)}x to {SPEED.max.toFixed(2)}x
                </span>
              </div>
              <input
                id="agent-tts-speed"
                type="range"
                min={SPEED.min}
                max={SPEED.max}
                step={SPEED.step}
                value={formData.tts_speed}
                onChange={(e) =>
                  setFormData({
                    ...formData,
                    tts_speed: parseFloat(e.target.value),
                  })
                }
                className="w-full accent-moss-400 h-2 bg-ink-800 rounded-lg cursor-pointer"
              />
            </div>
          </section>

          {/* Language model configuration */}
          <section className="border-b border-ink-800 py-6">
            <h2 className="mb-5 text-lg font-semibold text-limestone-100">Language model</h2>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
              <div>
                <label htmlFor="agent-model" className="field-label">
                  Model
                </label>
                <select
                  id="agent-model"
                  value={formData.llm_model}
                  onChange={(e) => {
                    modelTouchedRef.current = true;
                    setFormData({ ...formData, llm_model: e.target.value });
                  }}
                  className="field-control"
                >
                  {resolveModelOptions(formData.llm_model, catalog.llm_models).map(
                    (m) => (
                      <option key={m.id} value={m.id}>
                        {m.name}
                      </option>
                    )
                  )}
                </select>
                <p className="text-[11px] text-limestone-500 mt-1.5">
                  Model used when this agent handles a new session.
                </p>
              </div>

              <div>
                <label htmlFor="agent-reasoning" className="field-label">
                  Reasoning Effort Policy
                </label>
                <select
                  id="agent-reasoning"
                  value={formData.llm_reasoning_effort}
                  onChange={(e) =>
                    setFormData({
                      ...formData,
                      llm_reasoning_effort: e.target.value,
                    })
                  }
                  className="field-control"
                >
                  <option value="">Use model default</option>
                  <option value="none">
                    none (suppress hidden thinking tokens for voice)
                  </option>
                  <option value="low">low (brief CoT)</option>
                  <option value="medium">medium</option>
                  <option value="high">high</option>
                </select>
                <p className="text-[11px] text-limestone-500 mt-1.5">
                  Choose whether this agent uses additional reasoning effort.
                </p>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-6 pt-2">
              <div>
                <div className="flex justify-between items-center mb-1.5">
                  <label htmlFor="agent-temperature" className="field-label">
                    Temperature:{" "}
                    <span className="text-copper-400 font-mono font-bold">
                      {formData.llm_temperature.toFixed(2)}
                    </span>
                  </label>
                  <span className="text-[11px] text-limestone-500 font-mono">
                    {formData.llm_temperature !== undefined &&
                    formData.llm_temperature < 0.4
                      ? "Precise / Factual"
                      : formData.llm_temperature !== undefined &&
                        formData.llm_temperature < 0.8
                      ? "Conversational"
                      : "Creative"}
                  </span>
                </div>
                <input
                  id="agent-temperature"
                  type="range"
                  min={TEMPERATURE.min}
                  max={TEMPERATURE.max}
                  step={TEMPERATURE.step}
                  value={formData.llm_temperature}
                  onChange={(e) =>
                    setFormData({
                      ...formData,
                      llm_temperature: parseFloat(e.target.value),
                    })
                  }
                  className="w-full accent-copper-400 h-2 bg-ink-800 rounded-lg cursor-pointer"
                />
              </div>

              <div>
                <div className="flex justify-between items-center mb-1.5">
                  <label htmlFor="agent-max-tokens" className="field-label">
                    Max Spoken Tokens:{" "}
                    <span className="text-copper-400 font-mono font-bold">
                      {formData.llm_max_tokens}
                    </span>
                  </label>
                  <span className="text-[11px] text-limestone-500 font-mono">
                    Cap per turn
                  </span>
                </div>
                <input
                  id="agent-max-tokens"
                  type="range"
                  min={MAX_TOKENS.min}
                  max={MAX_TOKENS.max}
                  step={MAX_TOKENS.step}
                  value={formData.llm_max_tokens}
                  onChange={(e) =>
                    setFormData({
                      ...formData,
                      llm_max_tokens: parseInt(e.target.value),
                    })
                  }
                  className="w-full accent-copper-400 h-2 bg-ink-800 rounded-lg cursor-pointer"
                />
              </div>
            </div>
          </section>

          {/* Speech recognition configuration */}
          <section className="py-6">
            <h2 className="mb-5 text-lg font-semibold text-limestone-100">Speech recognition</h2>

            <fieldset className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <legend className="sr-only">Speech recognition provider</legend>
              {catalog.stt_providers.map((p) => {
                const isSelected = formData.stt_provider === p.id;
                return (
                  <label
                    key={p.id}
                    className={`w-full rounded-lg p-4 text-left cursor-pointer border transition-colors ${
                      isSelected
                        ? "bg-copper-500/10 border-copper-500/40 text-limestone-100"
                        : "bg-ink-950/60 border-ink-800/80 text-limestone-400 hover:border-ink-700"
                    }`}
                  >
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-sm font-medium text-limestone-100">
                        {p.name}
                      </span>
                      <input
                        type="radio"
                        name="stt-provider"
                        value={p.id}
                        checked={isSelected}
                        onChange={() => setFormData({ ...formData, stt_provider: p.id })}
                        className="ml-3 h-4 w-4 shrink-0 accent-copper-500"
                      />
                    </div>
                    <p className="text-xs text-limestone-400">{p.description}</p>
                  </label>
                );
              })}
            </fieldset>
          </section>
        </div>

      </form>

      {/* Real WebRTC Voice Test Modal */}
      {activeAgentForTesting && (
        <VoiceTestModal
          agent={activeAgentForTesting}
          isOpen={voiceModalOpen}
          onClose={() => setVoiceModalOpen(false)}
          onActivated={handleActivated}
        />
      )}
    </div>
  );
}
