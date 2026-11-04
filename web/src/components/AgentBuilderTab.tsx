"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Bot,
  ChevronDown,
  ChevronRight,
  Check,
  Sparkles,
} from "lucide-react";
import {
  platformAgentApi,
  type AgentDiagnostic,
  type CatalogLlmModel,
  type JsonObject,
  type PlatformCatalogView,
} from "@/lib/platform-agent-client";
import type { FlowDocument } from "@/lib/platform-agent-model";
import { ConversationFlowEditor } from "@/components/ConversationFlowEditor";
import { SttDropdown, TtsDropdown } from "@/components/SpeechProviderDropdowns";
import { getString } from "@/lib/agent-workspace-constants";

export interface DynamicModelItem {
  id: string;
  name: string;
  selectable: boolean;
  badge?: string;
}

export interface DynamicBrandGroup {
  id: string;
  name: string;
  models: DynamicModelItem[];
}

function groupModelsByProvider(models: CatalogLlmModel[]): DynamicBrandGroup[] {
  const groups = new Map<string, DynamicBrandGroup>();
  for (const model of models) {
    if (!model.provider_id || !model.provider_name) continue;
    const group = groups.get(model.provider_id) ?? {
      id: model.provider_id,
      name: model.provider_name,
      models: [],
    };
    group.models.push({
      id: model.id,
      name: model.name || model.id,
      selectable: model.selectable === true,
      badge: model.supports_reasoning ? "Reasoning" : undefined,
    });
    groups.set(group.id, group);
  }
  return [...groups.values()];
}

export interface AgentBuilderTabProps {
  identityDescription: string;
  identityPersona: string;
  onIdentityDescriptionChange: (value: string) => void;
  onIdentityPersonaChange: (value: string) => void;
  identityDisabled: boolean;
  identityError?: string | null;
  config: JsonObject;
  onLlmSelectionChange: (provider: string, model: string) => void;
  onSttSelectionChange: (provider: string, model: string) => void;
  textOnly: boolean;
  onVoiceSettingsChange: (provider: string, model: string, en: string, hi: string, textOnly: boolean) => void;
  flow: FlowDocument | null;
  flowError: string | null;
  visibleDiagnostics: AgentDiagnostic[];
  onFlowChange: (flow: FlowDocument) => void;
  onFlowJsonDirtyChange: (dirty: boolean) => void;
}

export function AgentBuilderTab({
  identityDescription,
  identityPersona,
  onIdentityDescriptionChange,
  onIdentityPersonaChange,
  identityDisabled,
  identityError,
  config,
  onLlmSelectionChange,
  onSttSelectionChange,
  textOnly,
  onVoiceSettingsChange,
  flow,
  flowError,
  visibleDiagnostics,
  onFlowChange,
  onFlowJsonDirtyChange,
}: AgentBuilderTabProps) {
  const currentModel = getString(config, "llm_model");
  const currentLlmProvider = typeof config.llm_provider === "string" ? config.llm_provider : "";
  const currentStt = getString(config, "stt_provider");
  const currentSttModel = typeof config.stt_model === "string" ? config.stt_model : "";
  const currentTts = typeof config.tts_provider === "string" ? config.tts_provider : "";
  const currentTtsModel = typeof config.tts_model === "string" ? config.tts_model : "";
  const currentVoiceEn = getString(config, "tts_voice_en");
  const currentVoiceHi = getString(config, "tts_voice_hi");

  // Dynamically fetched catalog from the platform implementation
  const [catalog, setCatalog] = useState<PlatformCatalogView | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogVersion, setCatalogVersion] = useState(0);

  useEffect(() => {
    let active = true;
    setCatalogError(null);
    void platformAgentApi.getCatalog().then((cat) => {
      if (active) setCatalog(cat);
    }).catch((error: unknown) => {
      if (active) {
        setCatalog(null);
        setCatalogError(error instanceof Error ? error.message : "Unable to load the catalog.");
      }
    });
    return () => {
      active = false;
    };
  }, [catalogVersion]);

  const brandGroups = useMemo(() => {
    return groupModelsByProvider(catalog?.llm_models || []);
  }, [catalog]);

  // Initial brand and model selection
  const initialBrandId = brandGroups[0]?.id || "";
  const [selectedBrandId, setSelectedBrandId] = useState<string>(initialBrandId);
  const [activeModelId, setActiveModelId] = useState<string>(currentModel);

  // Sync selected brand when brandGroups change
  useEffect(() => {
    if (brandGroups.length > 0 && !brandGroups.some((b) => b.id === selectedBrandId)) {
      setSelectedBrandId(brandGroups[0].id);
    }
  }, [brandGroups, selectedBrandId]);

  const [modelDropdownOpen, setModelDropdownOpen] = useState(false);


  // Active brand and active previewed model
  const activeBrand = brandGroups.find((b) => b.id === selectedBrandId) || brandGroups[0];
  const activeModelOption =
    activeBrand?.models.find((m) => m.id === activeModelId) ||
    activeBrand?.models[0];

  const currentActiveModelOption = brandGroups.find((b) => b.id === currentLlmProvider)
    ?.models.find((m) => m.id === currentModel);

  const handleSelectModel = (modelId: string) => {
    if (activeBrand) onLlmSelectionChange(activeBrand.id, modelId);
  };

  return (
    <div role="tabpanel" id="tabpanel-builder" aria-labelledby="tab-builder" className="space-y-6">
      {/* Sleek Horizontal Pipeline Toolbar */}
      <section aria-label="Pipeline Configuration" className="space-y-2">
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="text-xs font-semibold uppercase tracking-wider text-limestone-400 mr-1">
            Pipeline:
          </span>

          {/* 1. LLM Model Pill Button with Dynamically-Grouped Multi-Section Dropdown */}
          <div className="relative">
            <button
              type="button"
              onClick={() => {
                setModelDropdownOpen((prev) => !prev);
                setActiveModelId(currentModel);
                const b = brandGroups.find((brand) =>
                  brand.id === currentLlmProvider && brand.models.some((m) => m.id === currentModel)
                );
                if (b) setSelectedBrandId(b.id);
              }}
              className={`inline-flex items-center gap-2 rounded-lg border px-3.5 py-2 text-xs font-medium transition-colors ${
                modelDropdownOpen
                  ? "border-copper-500 bg-copper-950/30 text-copper-200"
                  : "border-ink-700 bg-ink-900/80 text-limestone-200 hover:border-ink-600 hover:bg-ink-800/80"
              }`}
              title="Configure LLM model"
            >
              <Bot className="h-3.5 w-3.5 text-copper-400" />
              <span>
                LLM:{" "}
                <strong className="font-mono text-limestone-100">
                  {currentActiveModelOption ? currentActiveModelOption.name : currentModel || "Platform default"}
                </strong>
              </span>
              <ChevronDown
                className={`h-3.5 w-3.5 text-limestone-400 transition-transform ${
                  modelDropdownOpen ? "rotate-180" : ""
                }`}
              />
            </button>

            {/* Cascading Multi-Section Dropdown: Section 1 Company -> Section 2 Models -> Section 3 Config */}
            {modelDropdownOpen ? (
              <>
                <div
                  className="fixed inset-0 z-30"
                  onClick={() => setModelDropdownOpen(false)}
                />
                <div className="absolute left-0 top-full mt-2 z-40 surface border-ink-700 bg-ink-950 shadow-2xl rounded-xl flex flex-col md:flex-row overflow-hidden divide-y md:divide-y-0 md:divide-x divide-ink-800 max-w-[95vw]">
                  {/* Section 1: Company / Origin (Hover or Click to open Section 2) */}
                  <div className="w-full md:w-40 p-3 space-y-1 bg-ink-950/90 shrink-0">
                    <span className="text-xs font-semibold text-limestone-400 block mb-2 px-1 uppercase tracking-wider">
                      Provider
                    </span>
                    <div className="space-y-1">
                      {brandGroups.map((brand) => {
                        const isBrandActive = brand.id === selectedBrandId;
                        return (
                          <button
                            key={brand.id}
                            type="button"
                            onMouseEnter={() => {
                              setSelectedBrandId(brand.id);
                              if (!brand.models.some((m) => m.id === activeModelId)) {
                                setActiveModelId(brand.models[0].id);
                              }
                            }}
                            onClick={() => {
                              setSelectedBrandId(brand.id);
                              if (!brand.models.some((m) => m.id === activeModelId)) {
                                setActiveModelId(brand.models[0].id);
                              }
                            }}
                            className={`w-full flex items-center justify-between px-2.5 py-2 rounded-lg text-xs font-medium transition-colors ${
                              isBrandActive
                                ? "bg-copper-950/40 text-copper-200 border border-copper-700/60 font-semibold"
                                : "text-limestone-400 hover:text-limestone-200 hover:bg-ink-900 border border-transparent"
                            }`}
                          >
                            <span>{brand.name}</span>
                            <ChevronRight
                              className={`h-3.5 w-3.5 ${
                                isBrandActive ? "text-copper-400" : "text-limestone-600"
                              }`}
                            />
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  {/* Section 2: Models for Company (Hover or Click to open Section 3) */}
                  <div className="w-full md:w-56 p-3 space-y-1.5 bg-ink-900/40 shrink-0">
                    <div className="flex items-center justify-between px-1 mb-2">
                      <span className="text-xs font-semibold text-limestone-400 uppercase tracking-wider">
                        {activeBrand ? activeBrand.name : "Models"}
                      </span>
                    </div>

                    <div className="space-y-1.5 max-h-[240px] overflow-y-auto pr-0.5">
                      {activeBrand?.models.map((model) => {
                        const isHoveredModel = activeModelId === model.id;
                        const isCurrentlyApplied = currentLlmProvider === activeBrand?.id && currentModel === model.id;
                        return (
                          <button
                            type="button"
                            key={model.id}
                            disabled={!model.selectable}
                            onMouseEnter={() => setActiveModelId(model.id)}
                            onClick={() => setActiveModelId(model.id)}
                            className={`w-full text-left p-2.5 rounded-lg text-xs transition-colors border flex items-center justify-between disabled:cursor-not-allowed disabled:opacity-50 ${
                              isHoveredModel
                                ? "bg-copper-950/30 border-copper-600/70 text-limestone-100"
                                : "border-ink-800/80 bg-ink-900/50 text-limestone-300 hover:border-ink-700 hover:bg-ink-900"
                            }`}
                          >
                            <div className="min-w-0 pr-2">
                              <div className="flex items-center gap-1.5">
                                <span className="font-semibold text-limestone-100 truncate">
                                  {model.name}
                                </span>
                                {isCurrentlyApplied ? (
                                  <span className="rounded bg-moss-950 border border-moss-800 px-1 py-0.2 text-xs font-mono text-moss-300">
                                    Active
                                  </span>
                                ) : null}
                                {!model.selectable ? <span className="text-limestone-400">Unavailable</span> : null}
                              </div>
                              <p className="text-xs text-limestone-500 font-mono mt-0.5 truncate">
                                {model.id}
                              </p>
                            </div>
                            <ChevronRight
                              className={`h-3.5 w-3.5 shrink-0 ${
                                isHoveredModel ? "text-copper-400" : "text-limestone-600"
                              }`}
                            />
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  {/* Section 3: Simplified Model Configuration (No Max Tokens, No Filler Text) */}
                  <div className="w-full md:w-64 p-3.5 space-y-3 bg-ink-950 shrink-0">
                    <div className="flex items-center justify-between border-b border-ink-800 pb-2">
                      <div className="flex items-center gap-1.5 text-copper-400">
                        <Sparkles className="h-4 w-4" />
                        <span className="text-xs font-semibold text-limestone-200 truncate">
                          {activeModelOption?.name || "No models available"}
                        </span>
                      </div>
                      {activeModelOption?.badge ? (
                        <span className="rounded bg-ink-800 px-1.5 py-0.5 text-xs font-mono text-copper-300">
                          {activeModelOption.badge}
                        </span>
                      ) : null}
                    </div>


                    {/* Action button */}
                    <div className="pt-1">
                      <button
                        type="button"
                        className="button-primary text-xs py-2 px-3 w-full"
                        disabled={!activeModelOption?.selectable}
                        onClick={() => {
                          if (activeModelOption?.selectable) handleSelectModel(activeModelOption.id);
                          setModelDropdownOpen(false);
                        }}
                      >
                        <Check className="h-3.5 w-3.5" />
                        {currentLlmProvider === activeBrand?.id && currentModel === activeModelOption?.id
                          ? "Apply Configuration"
                          : `Select ${activeModelOption?.name || "model"}`}
                      </button>
                    </div>
                  </div>
                </div>
              </>
            ) : null}
          </div>

          <SttDropdown
            providers={catalog?.stt_providers ?? []}
            providerId={currentStt}
            modelId={currentSttModel}
            onSelect={onSttSelectionChange}
          />
          <TtsDropdown
            providers={catalog?.tts_providers ?? []}
            providerId={currentTts}
            modelId={currentTtsModel}
            voiceEn={currentVoiceEn}
            voiceHi={currentVoiceHi}
            textOnly={textOnly}
            onSelect={onVoiceSettingsChange}
          />
        </div>
        {catalogError ? (
          <p role="alert" className="text-xs text-rust-300">
            Catalog unavailable: {catalogError}{" "}
            <button type="button" className="underline" onClick={() => setCatalogVersion((value) => value + 1)}>
              Retry
            </button>
          </p>
        ) : null}
      </section>

      {/* Persona Instructions (System Prompt) */}
      <section className="surface p-5 sm:p-6 space-y-4">
        <div className="space-y-4">
          <div>
            <label className="field-label" htmlFor="workspace-agent-description">
              Agent Purpose
            </label>
            <input
              id="workspace-agent-description"
              className="field-control"
              value={identityDescription}
              onChange={(e) => onIdentityDescriptionChange(e.currentTarget.value)}
              disabled={identityDisabled}
              placeholder="e.g. Handles billing verification and account assistance in Hindi & English"
            />
          </div>

          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className="field-label mb-0" htmlFor="workspace-agent-persona">
                Persona Instructions (System Prompt)
              </label>
              <span className="text-xs font-mono text-limestone-500">
                {identityPersona.length} / 20,000 chars
              </span>
            </div>
            <textarea
              id="workspace-agent-persona"
              className="field-control min-h-36 font-mono text-xs leading-relaxed"
              value={identityPersona}
              onChange={(e) => onIdentityPersonaChange(e.currentTarget.value)}
              disabled={identityDisabled}
              placeholder="Instructions that govern tone, boundary rules, policy constraints, and language switching..."
            />
          </div>

          {identityError ? <p role="alert" className="text-xs text-rust-300">{identityError}</p> : null}
        </div>
      </section>

      {/* Conversation Flow */}
      <section className="space-y-3">
        {flow ? (
          <ConversationFlowEditor
            flow={flow}
            diagnostics={visibleDiagnostics}
            onChange={onFlowChange}
            onDirtyChange={onFlowJsonDirtyChange}
          />
        ) : (
          <div className="surface p-5 space-y-3">
            <h2 className="text-base font-semibold text-limestone-100">Flow document unavailable</h2>
            <p role="alert" className="text-xs text-rust-300">
              {flowError ?? "The draft has no flow configuration."}
            </p>
            <p className="text-xs text-limestone-400">
              Your saved configuration has not been replaced. Correct its flow envelope through the platform draft contract.
            </p>
          </div>
        )}
      </section>

    </div>
  );
}
