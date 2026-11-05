"use client";

import { useState } from "react";
import type { Agent, AgentPatch, ProviderCatalog } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

export type AgentDraft = Pick<Agent,
  | "name"
  | "slug"
  | "description"
  | "instructions"
  | "languages"
  | "default_language"
  | "stt_provider"
  | "stt_model"
  | "llm_provider"
  | "llm_model"
  | "tts_provider"
  | "tts_model"
  | "voice_id"
>;

export function agentDraftFromAgent(agent: Agent): AgentDraft {
  return {
    name: agent.name,
    slug: agent.slug,
    description: agent.description,
    instructions: agent.instructions,
    languages: [...agent.languages],
    default_language: agent.default_language,
    stt_provider: agent.stt_provider,
    stt_model: agent.stt_model,
    llm_provider: agent.llm_provider,
    llm_model: agent.llm_model,
    tts_provider: agent.tts_provider,
    tts_model: agent.tts_model,
    voice_id: agent.voice_id,
  };
}

function draftChanges(draft: AgentDraft): AgentPatch {
  return {
    ...draft,
    name: draft.name.trim(),
    slug: draft.slug.trim(),
    description: draft.description?.trim() || null,
    instructions: draft.instructions?.trim() || null,
  };
}

function currentValueOption(value: string | null, label: string): string | null {
  if (!value) return null;
  return `${label} (current selection, not verified)`;
}

export function AgentOverview({ draft, onPatch }: { draft: AgentDraft; onPatch: (patch: Partial<AgentDraft>) => void }) {
  return (
    <div className="max-w-3xl space-y-5">
      <div>
        <h3 className="text-lg font-semibold">Agent overview</h3>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">Set a clear name, public URL, and short description.</p>
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor="agent-name">Name</Label>
          <Input id="agent-name" maxLength={120} value={draft.name} onChange={(event) => onPatch({ name: event.currentTarget.value })} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="agent-slug">Public URL slug</Label>
          <div className="flex min-h-11 items-center gap-2 rounded-xl border bg-background px-3.5 focus-within:ring-2 focus-within:ring-ring">
            <span className="shrink-0 text-sm text-muted-foreground">/a/</span>
            <input
              id="agent-slug"
              className="min-w-0 flex-1 bg-transparent py-2 text-sm outline-none"
              maxLength={80}
              pattern="[a-z0-9]+(-[a-z0-9]+)*"
              value={draft.slug}
              onChange={(event) => onPatch({ slug: event.currentTarget.value.toLocaleLowerCase().replace(/[^a-z0-9-]/g, "") })}
              aria-describedby="agent-slug-help"
            />
          </div>
          <p id="agent-slug-help" className="text-xs leading-5 text-muted-foreground">The slug must be unique. Changing it updates the public call link.</p>
        </div>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="agent-description">Description</Label>
        <Textarea id="agent-description" maxLength={500} value={draft.description ?? ""} onChange={(event) => onPatch({ description: event.currentTarget.value })} />
        <p className="text-xs text-muted-foreground">This description may be shown on the public call page.</p>
      </div>
    </div>
  );
}

export function AgentVoice({
  draft,
  catalog,
  catalogError,
  onPatch,
  onRetryCatalog,
}: {
  draft: AgentDraft;
  catalog: ProviderCatalog | null;
  catalogError: string | null;
  onPatch: (patch: Partial<AgentDraft>) => void;
  onRetryCatalog: () => void;
}) {
  const openAiAvailableModels = catalog?.openai.models.filter((model) => model.available) ?? [];
  const selectedProvider = catalog?.tts_providers.find((provider) => provider.provider === draft.tts_provider) ?? null;
  const availableTtsProviders = catalog?.tts_providers.filter((provider) => provider.available) ?? [];
  const availableModels = selectedProvider?.available
    ? selectedProvider.models.filter((model) => model.available)
    : [];
  const selectedModel = selectedProvider?.models.find((model) => model.id === draft.tts_model) ?? null;
  const availableVoices = selectedModel?.available ? selectedModel.voices.filter((voice) => voice.available) : [];
  const selectedVoice = selectedModel?.voices.find((voice) => voice.id === draft.voice_id) ?? null;
  const unavailableProviderOption = draft.tts_provider && !availableTtsProviders.some((provider) => provider.provider === draft.tts_provider)
    ? draft.tts_provider
    : null;
  const unavailableModelOption = draft.tts_model && !availableModels.some((model) => model.id === draft.tts_model)
    ? draft.tts_model
    : null;
  const unavailableVoiceOption = draft.voice_id && !availableVoices.some((voice) => voice.id === draft.voice_id)
    ? draft.voice_id
    : null;

  return (
    <div className="max-w-3xl space-y-8">
      <div>
        <h3 className="text-lg font-semibold">AI and voice</h3>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">Choose only models and voices verified by the server. Credentials stay on the backend.</p>
      </div>
      {catalogError ? (
        <div role="alert" className="flex flex-col gap-3 rounded-xl border border-amber-700/25 bg-amber-700/10 p-4 text-sm text-amber-950 dark:text-amber-200 sm:flex-row sm:items-center sm:justify-between">
          <p>{catalogError}</p>
          <Button variant="outline" size="sm" onClick={onRetryCatalog}>Retry provider catalog</Button>
        </div>
      ) : null}

      <section className="space-y-4 border-t pt-6" aria-labelledby="stt-heading">
        <div>
          <h4 id="stt-heading" className="font-semibold">Speech recognition</h4>
          <p className="mt-1 text-sm text-muted-foreground">Only Deepgram Nova-3 is configured by this runtime.</p>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="stt-provider">Provider</Label>
            <Select
              id="stt-provider"
              value={draft.stt_provider ?? ""}
              onChange={(event) => onPatch({ stt_provider: event.currentTarget.value || null, stt_model: event.currentTarget.value ? "nova-3" : null })}
            >
              <option value="">Select a provider</option>
              {draft.stt_provider && draft.stt_provider !== "deepgram" ? <option value={draft.stt_provider} disabled>{currentValueOption(draft.stt_provider, draft.stt_provider)}</option> : null}
              <option value="deepgram">Deepgram</option>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="stt-model">Model</Label>
            <Select id="stt-model" value={draft.stt_model ?? ""} onChange={(event) => onPatch({ stt_model: event.currentTarget.value || null })}>
              <option value="">Select a model</option>
              {draft.stt_model && draft.stt_model !== "nova-3" ? <option value={draft.stt_model} disabled>{currentValueOption(draft.stt_model, draft.stt_model)}</option> : null}
              <option value="nova-3">Nova-3</option>
            </Select>
          </div>
        </div>
      </section>

      <section className="space-y-4 border-t pt-6" aria-labelledby="llm-heading">
        <div>
          <h4 id="llm-heading" className="font-semibold">Language model</h4>
          <p className="mt-1 text-sm text-muted-foreground">The model list contains verified choices only.</p>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="llm-provider">Provider</Label>
            <Select
              id="llm-provider"
              value={draft.llm_provider ?? ""}
              onChange={(event) => onPatch({ llm_provider: event.currentTarget.value || null, llm_model: null })}
            >
              <option value="">Select a provider</option>
              {draft.llm_provider && draft.llm_provider !== "openai" ? <option value={draft.llm_provider} disabled>{currentValueOption(draft.llm_provider, draft.llm_provider)}</option> : null}
              <option value="openai" disabled={catalog?.openai.available !== true}>OpenAI{catalog?.openai.available ? "" : " (unavailable)"}</option>
            </Select>
            {catalog?.openai.error ? <p className="text-xs text-destructive">{catalog.openai.error}</p> : null}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="llm-model">Model</Label>
            <Select id="llm-model" value={draft.llm_model ?? ""} onChange={(event) => onPatch({ llm_model: event.currentTarget.value || null })}>
              <option value="">Select a verified model</option>
              {draft.llm_model && !openAiAvailableModels.some((model) => model.id === draft.llm_model)
                ? <option value={draft.llm_model} disabled>{currentValueOption(draft.llm_model, draft.llm_model)}</option>
                : null}
              {openAiAvailableModels.map((model) => <option key={model.id} value={model.id}>{model.id}</option>)}
            </Select>
            {catalog?.openai.available === false ? <p className="text-xs text-muted-foreground">No verified OpenAI model is currently available.</p> : null}
          </div>
        </div>
      </section>

      <section className="space-y-4 border-t pt-6" aria-labelledby="tts-heading">
        <div>
          <h4 id="tts-heading" className="font-semibold">Voice output</h4>
          <p className="mt-1 text-sm text-muted-foreground">Choose an available provider, model, and voice. Voice language support is shown in the Languages tab.</p>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="tts-provider">Provider</Label>
            <Select
              id="tts-provider"
              value={draft.tts_provider ?? ""}
              onChange={(event) => {
                const provider = catalog?.tts_providers.find((item) => item.provider === event.currentTarget.value && item.available)?.provider ?? null;
                onPatch({ tts_provider: provider, tts_model: null, voice_id: null, languages: [], default_language: null });
              }}
            >
              <option value="">Select a verified provider</option>
              {unavailableProviderOption ? <option value={unavailableProviderOption} disabled>{unavailableProviderOption} (unavailable)</option> : null}
              {availableTtsProviders.map((provider) => <option key={provider.provider} value={provider.provider}>{provider.provider === "cartesia" ? "Cartesia" : "ElevenLabs"}</option>)}
            </Select>
            {selectedProvider?.error ? <p className="text-xs text-destructive">{selectedProvider.error}</p> : null}
            {catalog && !selectedProvider && availableTtsProviders.length === 0 ? <p className="text-xs text-muted-foreground">No verified text-to-speech provider is available.</p> : null}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="tts-model">Model</Label>
            <Select
              id="tts-model"
              value={draft.tts_model ?? ""}
              onChange={(event) => {
                const model = availableModels.find((item) => item.id === event.currentTarget.value);
                onPatch({ tts_model: model?.id ?? null, voice_id: null, languages: [], default_language: null });
              }}
              disabled={!selectedProvider?.available}
            >
              <option value="">Select a verified model</option>
              {unavailableModelOption ? <option value={unavailableModelOption} disabled>{unavailableModelOption} (unavailable)</option> : null}
              {availableModels.map((model) => <option key={model.id} value={model.id}>{model.id}</option>)}
            </Select>
          </div>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="voice-id">Voice</Label>
          <Select
            id="voice-id"
            value={draft.voice_id ?? ""}
            onChange={(event) => {
              const voice = availableVoices.find((item) => item.id === event.currentTarget.value);
              if (!voice) {
                onPatch({ voice_id: null, languages: [], default_language: null });
                return;
              }
              const languages = draft.languages.filter((language) => voice.languages.includes(language));
              const defaultLanguage = draft.default_language && languages.includes(draft.default_language) ? draft.default_language : languages[0] ?? null;
              onPatch({ voice_id: voice.id, languages, default_language: defaultLanguage });
            }}
            disabled={!selectedModel?.available}
          >
            <option value="">Select an available voice</option>
            {unavailableVoiceOption ? <option value={unavailableVoiceOption} disabled>{unavailableVoiceOption} (unavailable)</option> : null}
            {availableVoices.map((voice) => <option key={voice.id} value={voice.id}>{voice.id}</option>)}
          </Select>
          <p className="text-xs leading-5 text-muted-foreground">Changing voice keeps only languages supported by the new voice.</p>
        </div>
      </section>
    </div>
  );
}

export function AgentLanguages({
  draft,
  catalog,
  catalogError,
  onPatch,
}: {
  draft: AgentDraft;
  catalog: ProviderCatalog | null;
  catalogError: string | null;
  onPatch: (patch: Partial<AgentDraft>) => void;
}) {
  const provider = catalog?.tts_providers.find((item) => item.provider === draft.tts_provider) ?? null;
  const model = provider?.models.find((item) => item.id === draft.tts_model) ?? null;
  const voices = model?.voices.filter((voice) => voice.available) ?? [];
  const selectedVoice = voices.find((voice) => voice.id === draft.voice_id);
  const languages = selectedVoice
    ? selectedVoice.languages
    : [...new Set(voices.flatMap((voice) => voice.languages))].sort();
  const [selectionError, setSelectionError] = useState<string | null>(null);

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h3 className="text-lg font-semibold">Languages</h3>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">Languages come from verified voices for the selected provider and model.</p>
      </div>
      {catalogError ? <p role="status" className="rounded-xl border bg-muted/40 p-3 text-sm text-muted-foreground">Provider catalog is unavailable, so language choices cannot be verified yet.</p> : null}
      {!draft.voice_id || languages.length === 0 ? (
        <div className="rounded-xl border border-dashed p-4 text-sm leading-6 text-muted-foreground">
          Choose an available voice in AI and voice to see its supported languages.
        </div>
      ) : (
        <fieldset className="space-y-4 rounded-xl border p-4 sm:p-5">
          <legend className="px-1 text-sm font-semibold">Supported call language</legend>
          <p className="text-sm leading-6 text-muted-foreground">
            Language switching is not verified for the current runtime. Select one language for a publishable agent.
          </p>
          {languages.map((language) => (
            <label key={language} className="flex items-start gap-3 rounded-xl border bg-card p-3">
              <input
                type="checkbox"
                className="mt-0.5 size-4 rounded border-input accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                checked={draft.languages.includes(language)}
                onChange={(event) => {
                  setSelectionError(null);
                  if (event.currentTarget.checked) {
                    if (draft.languages.some((item) => item !== language)) {
                      setSelectionError("Only one call language is currently verified for a published agent.");
                      return;
                    }
                    onPatch({ languages: [language], default_language: language });
                  } else {
                    const nextLanguages = draft.languages.filter((item) => item !== language);
                    onPatch({ languages: nextLanguages, default_language: draft.default_language === language ? nextLanguages[0] ?? null : draft.default_language });
                  }
                }}
              />
              <span>
                <span className="block text-sm font-medium">{language === "en" ? "English" : language === "hi" ? "Hindi" : language}</span>
                <span className="mt-0.5 block text-xs text-muted-foreground">{language}</span>
              </span>
            </label>
          ))}
          {draft.languages.some((language) => !languages.includes(language)) ? (
            <p className="text-sm text-destructive">The selected voice does not support one or more saved languages. Remove them or choose a compatible voice.</p>
          ) : null}
          {selectionError ? <p role="alert" className="text-sm text-destructive">{selectionError}</p> : null}
        </fieldset>
      )}
      <div className="max-w-sm space-y-1.5">
        <Label htmlFor="default-language">Default language</Label>
        <Select
          id="default-language"
          value={draft.default_language ?? ""}
          disabled={draft.languages.length === 0}
          onChange={(event) => onPatch({ default_language: event.currentTarget.value || null })}
        >
          <option value="">Choose a default</option>
          {draft.languages.map((language) => <option key={language} value={language}>{language === "en" ? "English" : language === "hi" ? "Hindi" : language}</option>)}
        </Select>
      </div>
    </div>
  );
}

export function AgentInstructions({ draft, onPatch }: { draft: AgentDraft; onPatch: (patch: Partial<AgentDraft>) => void }) {
  return (
    <div className="max-w-3xl space-y-5">
      <div>
        <h3 className="text-lg font-semibold">Agent instructions</h3>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">Add agent-specific guidance. Global administrator policy has higher priority.</p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="agent-instructions">Instructions</Label>
        <Textarea
          id="agent-instructions"
          rows={10}
          value={draft.instructions ?? ""}
          onChange={(event) => onPatch({ instructions: event.currentTarget.value })}
          aria-describedby="agent-instructions-help"
        />
        <p id="agent-instructions-help" className="text-xs leading-5 text-muted-foreground">Only this agent's instructions are used when it is active. Keep provider credentials out of this field.</p>
      </div>
    </div>
  );
}

export { draftChanges };
