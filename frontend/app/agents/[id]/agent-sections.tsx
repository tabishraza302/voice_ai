"use client";

import { useMemo } from "react";
import type { Agent, AgentPatch, ProviderCatalog } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { MarkdownEditor } from "@/components/ui/markdown-editor";
import { PROMPT_PLACEHOLDER } from "@/lib/prompt-template";
import { TtsVoiceSelect, formatProviderName, type TtsProviderOption } from "@/components/ui/tts-voice-select";

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
  const allCatalogTtsProviders = catalog?.tts_providers ?? [];

  const ttsProviders: TtsProviderOption[] = useMemo(() => {
    return allCatalogTtsProviders.map((provider) => ({
      provider: provider.provider,
      label: formatProviderName(provider.provider),
      available: provider.available,
      error: provider.error,
      sampleUrl: provider.sample_url,
      voices: provider.models.flatMap((model) =>
        model.voices
          .filter((voice) => voice.available)
          .map((voice) => ({
            provider: provider.provider,
            providerLabel: formatProviderName(provider.provider),
            modelId: model.id,
            voiceId: voice.id,
            name: voice.name,
            languages: voice.languages,
            sampleUrl: voice.sample_url ?? provider.sample_url,
            label: `${formatProviderName(provider.provider)} - ${voice.name ? `${voice.name} (${voice.id})` : voice.id}`,
          }))
      ),
    }));
  }, [allCatalogTtsProviders]);

  const allVoices = useMemo(() => {
    return ttsProviders.flatMap((p) => p.voices);
  }, [ttsProviders]);

  const currentVoice = allVoices.find((v) => v.voiceId === draft.voice_id);
  const supportedLanguages = useMemo(() => {
    if (currentVoice) return currentVoice.languages;
    if (draft.languages.length > 0) return draft.languages;
    return ["en"];
  }, [currentVoice, draft.languages]);

  return (
    <div className="rounded-2xl border bg-card/75 p-4 sm:p-5">
      {catalogError ? (
        <div className="mb-3 flex items-center justify-between gap-2">
          <span className="text-xs text-amber-600">Could not load provider catalog</span>
          <Button variant="outline" size="sm" onClick={onRetryCatalog} className="text-xs text-amber-600">
            Retry Catalog
          </Button>
        </div>
      ) : null}

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
          <TtsVoiceSelect
            id="toolbar-tts"
            value={draft.voice_id ?? null}
            selectedProvider={draft.tts_provider}
            providers={ttsProviders}
            disabled={catalogLoading || ttsProviders.length === 0}
            onSelect={(found) => {
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
          />
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
  const instructions = draft.instructions ?? "";
  const charCount = instructions.length;

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h3 className="text-lg font-semibold tracking-tight">Agent System Prompt</h3>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Define the agent&apos;s full persona, questions, flow, tone, and guardrails using Markdown.
          </p>
        </div>
        <span className="inline-flex items-center rounded-full border border-border px-2.5 py-0.5 text-xs font-mono text-muted-foreground">
          {charCount.toLocaleString()} chars
        </span>
      </div>

      <div className="space-y-2">
        <MarkdownEditor
          id="agent-instructions"
          value={instructions}
          onChange={(val) => onPatch({ instructions: val })}
          placeholder={PROMPT_PLACEHOLDER}
          aria-describedby="agent-instructions-help"
        />
        <p id="agent-instructions-help" className="text-xs text-muted-foreground">
          This prompt is fed directly to the voice and chat model on every conversation turn. Use Markdown headings (###) for structure.
        </p>
      </div>
    </div>
  );
}
