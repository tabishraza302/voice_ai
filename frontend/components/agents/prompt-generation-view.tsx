"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, apiErrorText, streamSse } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { EnhancePromptModal } from "./enhance-prompt-modal";

interface EnhanceResponse {
  enhanced_prompt: string;
}

type MilestonePhase = "analyzing" | "instructions" | "call_flow" | "saving" | "complete";

const MILESTONES: Array<{ id: MilestonePhase; label: string }> = [
  { id: "analyzing", label: "Analyzing requirements" },
  { id: "instructions", label: "Drafting instructions" },
  { id: "call_flow", label: "Designing call flow" },
  { id: "saving", label: "Finalizing agent" },
];

export function PromptGenerationView({ onCancel }: { onCancel: () => void }) {
  const router = useRouter();
  const [prompt, setPrompt] = useState("");
  const [availableModels, setAvailableModels] = useState<string[]>(["gpt-4o-mini", "gpt-4.1-mini"]);
  const [selectedModel, setSelectedModel] = useState("gpt-4o-mini");
  const [enhancing, setEnhancing] = useState(false);
  const [enhanceModalOpen, setEnhanceModalOpen] = useState(false);
  const [enhancedResult, setEnhancedResult] = useState("");
  const [enhanceError, setEnhanceError] = useState<string | null>(null);

  // Generation state
  const [generating, setGenerating] = useState(false);
  const [activePhase, setActivePhase] = useState<MilestonePhase>("analyzing");
  const [streamedText, setStreamedText] = useState("");
  const [generationError, setGenerationError] = useState<string | null>(null);

  const typewriterBottomRef = useRef<HTMLDivElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  const canSubmit = prompt.trim().length >= 10;

  useEffect(() => {
    let current = true;
    api<{ openai?: { models?: Array<{ id: string; available: boolean }> } }>("/provider-catalog")
      .then((catalog) => {
        if (!current) return;
        const models = catalog.openai?.models
          ?.filter((m) => m.available)
          .map((m) => m.id);
        if (models && models.length > 0) {
          setAvailableModels(models);
          setSelectedModel((prev) => (models.includes(prev) ? prev : models[0]));
        }
      })
      .catch(() => {});
    return () => {
      current = false;
    };
  }, []);

  useEffect(() => {
    if (generating && typewriterBottomRef.current) {
      typewriterBottomRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [streamedText, generating]);

  useEffect(() => {
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, []);

  async function handleEnhance() {
    if (!canSubmit || enhancing) return;
    setEnhancing(true);
    setEnhanceError(null);
    try {
      const response = await api<EnhanceResponse>("/agents/enhance-prompt", {
        method: "POST",
        body: JSON.stringify({ prompt: prompt.trim(), model: selectedModel }),
      });
      setEnhancedResult(response.enhanced_prompt);
      setEnhanceModalOpen(true);
    } catch (err: unknown) {
      setEnhanceError(apiErrorText(err, "Could not enhance prompt. Try again."));
    } finally {
      setEnhancing(false);
    }
  }

  async function startGenerating() {
    if (!canSubmit || (generating && !generationError)) return;
    setGenerating(true);
    setGenerationError(null);
    setActivePhase("analyzing");
    setStreamedText("");

    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      await streamSse(
        "/agents/generate-stream",
        { prompt: prompt.trim(), model: selectedModel },
        (event, data) => {
          if (event === "milestone") {
            const payload = data as { phase?: MilestonePhase };
            if (payload?.phase) {
              setActivePhase(payload.phase);
            }
          } else if (event === "token") {
            const payload = data as { text?: string };
            if (payload?.text) {
              setStreamedText((current) => current + payload.text);
            }
          } else if (event === "complete") {
            const payload = data as { agent_id?: string };
            setActivePhase("complete");
            if (payload?.agent_id) {
              setTimeout(() => {
                router.replace(`/agents/${payload.agent_id}`);
              }, 400);
            }
          } else if (event === "error") {
            const payload = data as { message?: string };
            setGenerationError(payload?.message || "Generation failed.");
          }
        },
        controller.signal,
      );
    } catch (err: unknown) {
      if (!controller.signal.aborted) {
        setGenerationError(apiErrorText(err, "Agent generation interrupted."));
      }
    }
  }

  function phaseIndex(phase: MilestonePhase): number {
    if (phase === "complete") return MILESTONES.length;
    const idx = MILESTONES.findIndex((m) => m.id === phase);
    return idx === -1 ? 0 : idx;
  }

  const currentPhaseIndex = phaseIndex(activePhase);

  return (
    <div>
      {/* Enhance Modal */}
      <EnhancePromptModal
        open={enhanceModalOpen}
        originalPrompt={prompt}
        enhancedPrompt={enhancedResult}
        onApply={(updated) => setPrompt(updated)}
        onClose={() => setEnhanceModalOpen(false)}
      />

      {generating ? (
        <div className="space-y-6">
          {/* Milestone checklist bar on top */}
          <div className="rounded-2xl border bg-card p-5 sm:p-6 shadow-sm">
            <h2 className="text-base font-semibold tracking-tight mb-4 flex items-center gap-2">
              <span className="flex h-2.5 w-2.5 rounded-full bg-primary animate-ping" />
              Building your agent
            </h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {MILESTONES.map((milestone, idx) => {
                const isPassed = currentPhaseIndex > idx;
                const isCurrent = currentPhaseIndex === idx && !generationError;
                return (
                  <div
                    key={milestone.id}
                    className={`flex items-center gap-2.5 rounded-xl border p-3 text-xs transition-colors ${
                      isPassed
                        ? "border-primary/40 bg-primary/10 text-primary font-medium"
                        : isCurrent
                        ? "border-primary bg-background text-foreground font-semibold shadow-xs"
                        : "border-border/60 bg-muted/20 text-muted-foreground"
                    }`}
                  >
                    {isPassed ? (
                      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground text-[10px]">
                        ✓
                      </span>
                    ) : isCurrent ? (
                      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 border-primary border-t-transparent animate-spin" />
                    ) : (
                      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-border text-[10px]">
                        {idx + 1}
                      </span>
                    )}
                    <span className="truncate">{milestone.label}</span>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Error Banner if error occurred */}
          {generationError ? (
            <div
              role="alert"
              className="rounded-2xl border border-destructive/40 bg-destructive/10 p-5 text-destructive space-y-3"
            >
              <div className="flex items-start gap-2.5">
                <span className="text-lg">⚠</span>
                <div>
                  <h3 className="text-sm font-semibold">Generation encountered an error</h3>
                  <p className="mt-1 text-xs opacity-90">{generationError}</p>
                </div>
              </div>
              <div className="flex gap-2.5 pt-1">
                <Button variant="primary" size="sm" type="button" onClick={startGenerating}>
                  Retry generation
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  type="button"
                  onClick={() => {
                    setGenerating(false);
                    setGenerationError(null);
                  }}
                >
                  Back to prompt
                </Button>
              </div>
            </div>
          ) : null}

          {/* Typewriter streaming display in backdrop card */}
          <div className="rounded-2xl border bg-muted/20 p-5 sm:p-6 font-mono text-xs shadow-inner">
            <div className="flex items-center justify-between pb-3 border-b mb-3 text-muted-foreground text-[11px]">
              <span className="flex items-center gap-2">
                <span className="h-2 w-2 rounded-full bg-green-500 inline-block" />
                Live Agent Generation Stream
              </span>
              <span className="rounded-md bg-muted px-2 py-0.5 text-xs text-foreground font-mono">
                {selectedModel}
              </span>
            </div>
            <div className="max-h-[22rem] overflow-y-auto whitespace-pre-wrap leading-relaxed text-foreground/90">
              {streamedText ? (
                <>
                  {streamedText}
                  <span className="inline-block w-2 h-4 bg-primary align-middle ml-1 animate-pulse" />
                </>
              ) : (
                <span className="text-muted-foreground italic">
                  Connecting to {selectedModel} and generating agent...
                </span>
              )}
              <div ref={typewriterBottomRef} />
            </div>
          </div>
        </div>
      ) : (
        <form
          className="space-y-5"
          onSubmit={(e) => {
            e.preventDefault();
            startGenerating();
          }}
        >
          {enhanceError ? (
            <p
              role="alert"
              className="rounded-xl border border-destructive/30 bg-destructive/10 px-3.5 py-3 text-sm text-destructive"
            >
              {enhanceError}
            </p>
          ) : null}

          <div className="space-y-2">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
              <Label htmlFor="agent-prompt" className="text-sm font-medium">
                Describe the agent you want to build
              </Label>
              <div className="flex items-center gap-2">
                <Label htmlFor="model-select" className="text-xs text-muted-foreground whitespace-nowrap">
                  LLM Model:
                </Label>
                <select
                  id="model-select"
                  value={selectedModel}
                  onChange={(e) => setSelectedModel(e.target.value)}
                  className="rounded-lg border bg-background px-2.5 py-1 text-xs text-foreground outline-none focus:ring-1 focus:ring-primary shadow-xs font-mono"
                >
                  {availableModels.map((modelId) => (
                    <option key={modelId} value={modelId}>
                      {modelId}
                    </option>
                  ))}
                </select>
                <span className="text-xs text-muted-foreground ml-1">
                  {prompt.length} char{prompt.length === 1 ? "" : "s"}
                  {prompt.length > 0 && prompt.length < 10 ? " (min 10)" : ""}
                </span>
              </div>
            </div>
            <textarea
              id="agent-prompt"
              name="prompt"
              rows={6}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="e.g. A dental clinic receptionist who answers incoming calls, asks for the caller's name, books or reschedules appointments, answers common questions about parking and insurance, and confirms the final booking details."
              className="w-full rounded-xl border border-input bg-background p-4 text-sm leading-relaxed text-foreground shadow-xs placeholder:text-muted-foreground/60 focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary resize-y"
            />
            <p className="text-xs text-muted-foreground">
              {selectedModel} will generate the agent&apos;s name, voice instructions, and call flow steps from your prompt.
            </p>
          </div>

          <div className="flex flex-col-reverse gap-3 border-t pt-5 sm:flex-row sm:items-center sm:justify-between">
            <Button
              type="button"
              variant="outline"
              disabled={!canSubmit || enhancing}
              onClick={handleEnhance}
              className="flex items-center gap-2"
            >
              {enhancing ? (
                <>
                  <span className="h-4 w-4 rounded-full border-2 border-primary border-t-transparent animate-spin" />
                  Enhancing prompt...
                </>
              ) : (
                <>
                  <svg
                    className="w-4 h-4 text-primary"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={2}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M13 10V3L4 14h7v7l9-11h-7z"
                    />
                  </svg>
                  Enhance Prompt
                </>
              )}
            </Button>

            <div className="flex items-center gap-3 justify-end">
              <Button type="button" variant="outline" onClick={onCancel}>
                Cancel
              </Button>
              <Button type="submit" disabled={!canSubmit}>
                Create Agent
              </Button>
            </div>
          </div>
        </form>
      )}
    </div>
  );
}
