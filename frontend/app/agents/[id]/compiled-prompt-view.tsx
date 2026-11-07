"use client";

import { useMemo, useState } from "react";
import type { AgentDraft } from "./agent-sections";
import type { CallFlowStep } from "./call-flow-adapter";
import {
  compileSystemPrompt,
  compileTurnMessages,
  estimateTokens,
} from "./prompt-compiler";
import { Button } from "@/components/ui/button";
import { cn } from "@/components/ui/utils";

type PromptDisplayMode = "prompt" | "messages";

export function CompiledPromptView({
  draft,
  flowSteps,
  globalPrompt,
}: {
  draft: AgentDraft;
  flowSteps: CallFlowStep[];
  globalPrompt?: string;
}) {
  const [mode, setMode] = useState<PromptDisplayMode>("prompt");
  const [selectedStepIndex, setSelectedStepIndex] = useState(0);
  const [copied, setCopied] = useState(false);

  const fullPrompt = useMemo(() => {
    return compileSystemPrompt({ draft, flowSteps, globalPrompt });
  }, [draft, flowSteps, globalPrompt]);

  const turnMessages = useMemo(() => {
    return compileTurnMessages({
      draft,
      flowSteps,
      activeStepIndex: selectedStepIndex,
      globalPrompt,
    });
  }, [draft, flowSteps, selectedStepIndex, globalPrompt]);

  const messagesJson = useMemo(() => {
    return JSON.stringify(turnMessages, null, 2);
  }, [turnMessages]);

  const displayText = mode === "prompt" ? fullPrompt : messagesJson;
  const tokenCount = useMemo(() => estimateTokens(displayText), [displayText]);

  const totalRoutes = useMemo(() => {
    return flowSteps.reduce((acc, step) => acc + step.answers.length + 1, 0);
  }, [flowSteps]);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(displayText);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback if clipboard API is restricted
      const textarea = document.createElement("textarea");
      textarea.value = displayText;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    }
  }

  return (
    <div className="space-y-4">
      {/* Top control bar */}
      <div className="flex flex-col gap-3 rounded-2xl border bg-card p-4 sm:flex-row sm:items-center sm:justify-between sm:p-5">
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-foreground">Compiled LLM Prompt</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Live prompt generated from the Visual Flow decision tree, agent persona, and runtime policy.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* Mode switch */}
          <div
            role="group"
            aria-label="Prompt format view"
            className="inline-flex items-center gap-1 rounded-xl border bg-muted/40 p-1"
          >
            <Button
              variant="ghost"
              size="sm"
              aria-pressed={mode === "prompt"}
              onClick={() => setMode("prompt")}
              className={cn(
                "h-8 rounded-lg px-2.5 text-xs font-medium",
                mode === "prompt"
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:bg-transparent hover:text-foreground"
              )}
            >
              Full Flow Prompt
            </Button>
            <Button
              variant="ghost"
              size="sm"
              aria-pressed={mode === "messages"}
              onClick={() => setMode("messages")}
              className={cn(
                "h-8 rounded-lg px-2.5 text-xs font-medium",
                mode === "messages"
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:bg-transparent hover:text-foreground"
              )}
            >
              Turn Messages (JSON)
            </Button>
          </div>

          {/* Copy button */}
          <Button
            variant="outline"
            size="sm"
            onClick={handleCopy}
            className="h-8 gap-1.5 px-3 text-xs font-medium"
            aria-label="Copy prompt text"
          >
            {copied ? (
              <>
                <svg
                  className="h-3.5 w-3.5 text-primary"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <polyline points="20 6 9 17 4 12" />
                </svg>
                <span className="text-primary font-medium">Copied!</span>
              </>
            ) : (
              <>
                <svg
                  className="h-3.5 w-3.5 text-muted-foreground"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                  <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                </svg>
                <span>Copy</span>
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Metadata metrics bar */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="inline-flex items-center gap-1.5 rounded-lg border bg-muted/30 px-2.5 py-1 text-muted-foreground">
          <span className="h-1.5 w-1.5 rounded-full bg-primary" />
          <span>Model: <strong className="font-medium text-foreground">{draft.llm_model || "openai:gpt-4o-mini"}</strong></span>
        </span>
        <span className="inline-flex items-center gap-1.5 rounded-lg border bg-muted/30 px-2.5 py-1 text-muted-foreground">
          <span>Language: <strong className="font-medium text-foreground">{draft.default_language || "en"}</strong></span>
        </span>
        <span className="inline-flex items-center gap-1.5 rounded-lg border bg-muted/30 px-2.5 py-1 text-muted-foreground">
          <span>Flow: <strong className="font-medium text-foreground">{flowSteps.length} {flowSteps.length === 1 ? "step" : "steps"}</strong> ({totalRoutes} routes)</span>
        </span>
        <span className="inline-flex items-center gap-1.5 rounded-lg border bg-muted/30 px-2.5 py-1 text-muted-foreground">
          <span>Length: <strong className="font-medium text-foreground">~{tokenCount} tokens</strong> ({displayText.length} chars)</span>
        </span>
      </div>

      {/* Step selector for Turn Messages mode */}
      {mode === "messages" && flowSteps.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2 rounded-xl border bg-card/60 p-3">
          <span className="text-xs font-medium text-muted-foreground">Inspect Turn for Step:</span>
          <div className="flex flex-wrap gap-1.5">
            {flowSteps.map((step, idx) => (
              <button
                key={step.id}
                type="button"
                onClick={() => setSelectedStepIndex(idx)}
                className={cn(
                  "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                  selectedStepIndex === idx
                    ? "bg-primary text-primary-foreground shadow-sm"
                    : "border bg-muted/40 text-muted-foreground hover:bg-muted hover:text-foreground"
                )}
              >
                {`Step ${idx + 1}: ${step.title || "Untitled"}`}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {/* Code Display Area */}
      <div className="relative rounded-2xl border bg-card/90 shadow-sm">
        <div className="flex items-center justify-between border-b px-4 py-2.5 text-xs text-muted-foreground">
          <span className="font-medium">
            {mode === "prompt"
              ? "Synthesized Instructions (System & Conversation Flow)"
              : `OpenAI Chat Completion Payload (${turnMessages.length} Messages)`}
          </span>
          <span className="text-[11px] text-muted-foreground/80">
            {mode === "prompt" ? "Markdown / XML Directives" : "application/json"}
          </span>
        </div>

        <pre className="max-h-[640px] overflow-auto p-4 sm:p-5 font-mono text-xs leading-relaxed text-foreground/90 selection:bg-primary/20">
          <code>{displayText}</code>
        </pre>
      </div>
    </div>
  );
}
