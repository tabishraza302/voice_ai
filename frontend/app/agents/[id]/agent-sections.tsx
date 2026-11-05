"use client";

import { useMemo } from "react";
import type { Agent, AgentPatch, ProviderCatalog } from "@/lib/types";
import { Button } from "@/components/ui/button";
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

export function draftChanges(draft: AgentDraft): AgentPatch {
  const { slug: _slug, ...rest } = draft;
  return {
    ...rest,
    name: draft.name.trim(),
    description: draft.description?.trim() || null,
    instructions: draft.instructions?.trim() || null,
  };
}


export function AgentPipelineToolbar({
  draft,
  catalog,
  catalogError,
  catalogLoading,
  onPatch,
  onRetryCatalog,
}: {
  draft: AgentDraft;
  catalog: ProviderCatalog | null;
  catalogError: string | null;
  catalogLoading: boolean;
  onPatch: (patch: Partial<AgentDraft>) => void;
  onRetryCatalog: () => void;
}) {
  const openAiAvailableModels = catalog?.openai.models.filter((model) => model.available) ?? [];
  const availableTtsProviders = catalog?.tts_providers.filter((provider) => provider.available) ?? [];

  const allVoices = useMemo(() => {
    return availableTtsProviders.flatMap((provider) =>
      provider.models.flatMap((model) =>
        model.voices
          .filter((voice) => voice.available)
          .map((voice) => ({
            provider: provider.provider,
            modelId: model.id,
            voiceId: voice.id,
            languages: voice.languages,
            label: `${provider.provider === "cartesia" ? "Cartesia" : "ElevenLabs"} - ${voice.id}`,
          }))
      )
    );
  }, [availableTtsProviders]);

  const currentVoice = allVoices.find((v) => v.voiceId === draft.voice_id);
  const supportedLanguages = useMemo(() => {
    if (currentVoice) return currentVoice.languages;
    if (draft.languages.length > 0) return draft.languages;
    return ["en"];
  }, [currentVoice, draft.languages]);

  return (
    <div className="rounded-2xl border bg-card/75 p-4 sm:p-5">
      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h3 className="text-sm font-semibold tracking-tight text-foreground">AI Pipeline & Voice</h3>
          <p className="text-xs text-muted-foreground">Select speech recognition, language model, voice, and call language.</p>
        </div>
        {catalogError ? (
          <Button variant="outline" size="sm" onClick={onRetryCatalog} className="text-xs text-amber-600">
            Retry Catalog
          </Button>
        ) : null}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className="space-y-1.5">
          <Label htmlFor="toolbar-stt" className="text-xs font-medium text-muted-foreground">STT (Speech-to-Text)</Label>
          <Select
            id="toolbar-stt"
            value={draft.stt_provider ? `${draft.stt_provider}:${draft.stt_model ?? "nova-3"}` : ""}
            onChange={(e) => {
              const val = e.currentTarget.value;
              if (val === "deepgram:nova-3") {
                onPatch({ stt_provider: "deepgram", stt_model: "nova-3" });
              } else {
                onPatch({ stt_provider: null, stt_model: null });
              }
            }}
          >
            <option value="">Select STT</option>
            <option value="deepgram:nova-3">Deepgram (Nova-3)</option>
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="toolbar-llm" className="text-xs font-medium text-muted-foreground">LLM Model</Label>
          <Select
            id="toolbar-llm"
            value={draft.llm_model ?? ""}
            disabled={catalogLoading || openAiAvailableModels.length === 0}
            onChange={(e) => {
              const val = e.currentTarget.value;
              onPatch({ llm_provider: val ? "openai" : null, llm_model: val || null });
            }}
          >
            <option value="">Select Model</option>
            {openAiAvailableModels.map((model) => (
              <option key={model.id} value={model.id}>
                OpenAI: {model.id}
              </option>
            ))}
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="toolbar-tts" className="text-xs font-medium text-muted-foreground">TTS & Voice</Label>
          <Select
            id="toolbar-tts"
            value={draft.voice_id ?? ""}
            disabled={catalogLoading || allVoices.length === 0}
            onChange={(e) => {
              const val = e.currentTarget.value;
              const found = allVoices.find((v) => v.voiceId === val);
              if (found) {
                const nextLang =
                  draft.default_language && found.languages.includes(draft.default_language)
                    ? draft.default_language
                    : found.languages[0] ?? "en";
                onPatch({
                  tts_provider: found.provider,
                  tts_model: found.modelId,
                  voice_id: found.voiceId,
                  languages: [nextLang],
                  default_language: nextLang,
                });
              } else {
                onPatch({ tts_provider: null, tts_model: null, voice_id: null });
              }
            }}
          >
            <option value="">Select Voice</option>
            {allVoices.map((v) => (
              <option key={`${v.provider}-${v.voiceId}`} value={v.voiceId}>
                {v.label}
              </option>
            ))}
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="toolbar-lang" className="text-xs font-medium text-muted-foreground">Language</Label>
          <Select
            id="toolbar-lang"
            value={draft.default_language ?? ""}
            disabled={supportedLanguages.length === 0}
            onChange={(e) => {
              const val = e.currentTarget.value;
              onPatch({
                languages: val ? [val] : [],
                default_language: val || null,
              });
            }}
          >
            <option value="">Select Language</option>
            {supportedLanguages.map((lang) => (
              <option key={lang} value={lang}>
                {lang === "en" ? "English (en)" : lang === "hi" ? "Hindi (hi)" : lang}
              </option>
            ))}
          </Select>
        </div>
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
