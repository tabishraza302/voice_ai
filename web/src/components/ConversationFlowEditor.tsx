"use client";

import { useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import type { AgentDiagnostic, JsonObject, JsonValue } from "@/lib/platform-agent-client";
import {
  addFlowStep,
  deriveFlowGraph,
  getFlowSteps,
  removeFlowStep,
  updateFlowStep,
  type FlowDocument,
  type FlowGraph,
  type FlowStep,
} from "@/lib/platform-agent-model";

interface ConversationFlowEditorProps {
  flow: FlowDocument;
  diagnostics: AgentDiagnostic[];
  onChange: (flow: FlowDocument) => void;
}

const STEP_TYPES = ["ask", "collect", "inform", "action", "summary", "end"] as const;

function readText(value: JsonValue | undefined, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "The flow could not be read.";
}

export function ConversationFlowEditor({ flow, diagnostics, onChange }: ConversationFlowEditorProps) {
  const [view, setView] = useState<"steps" | "flow-json" | "flow-map">("steps");
  const [rawJson, setRawJson] = useState(() => JSON.stringify(flow, null, 2));
  const [rawError, setRawError] = useState<string | null>(null);
  useEffect(() => {
    setRawJson(JSON.stringify(flow, null, 2));
    setRawError(null);
  }, [flow]);

  let steps: FlowStep[] = [];
  let graph: FlowGraph = { nodes: [], edges: [] };
  let stepError: string | null = null;
  try {
    steps = getFlowSteps(flow);
    graph = deriveFlowGraph(flow);
  } catch (error: unknown) {
    stepError = errorText(error);
  }

  const updateRoot = (key: string, value: JsonValue) => {
    onChange({ ...flow, [key]: value });
  };

  const updateStep = (index: number, updates: Record<string, JsonValue>) => {
    onChange(updateFlowStep(flow, index, updates));
  };

  const addStep = () => {
    const currentSteps = getFlowSteps(flow);
    if (currentSteps.length >= 128) return;
    onChange(addFlowStep(flow));
  };

  const applyJson = () => {
    try {
      const parsed: unknown = JSON.parse(rawJson);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("The flow must be a JSON object.");
      }
      onChange(parsed as JsonObject);
      setRawError(null);
    } catch (error: unknown) {
      setRawError(errorText(error));
    }
  };

  return (
    <section aria-labelledby="flow-editor-heading" className="space-y-4">
      <div className="flex flex-col gap-4 border-b border-ink-800 pb-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 id="flow-editor-heading" className="text-xl font-semibold text-limestone-100">Conversation flow</h2>
          <p className="mt-1 text-sm text-limestone-400">Edit and validate the same versioned flow document used by Chat Test and publishing.</p>
        </div>
        <div role="tablist" aria-label="Flow editor view" className="flex flex-wrap gap-2">
          <button type="button" role="tab" aria-selected={view === "steps"} className={view === "steps" ? "button-primary" : "button-secondary"} onClick={() => setView("steps")}>Steps</button>
          <button type="button" role="tab" aria-selected={view === "flow-map"} className={view === "flow-map" ? "button-primary" : "button-secondary"} onClick={() => setView("flow-map")}>Flow view</button>
          <button type="button" role="tab" aria-selected={view === "flow-json"} className={view === "flow-json" ? "button-primary" : "button-secondary"} onClick={() => setView("flow-json")}>Flow JSON</button>
        </div>
      </div>

      {diagnostics.length > 0 ? (
        <div aria-label="Flow validation diagnostics" className="space-y-2">
          {diagnostics.map((diagnostic, index) => (
            <div
              key={`${diagnostic.code}-${diagnostic.path}-${index}`}
              className={`rounded-lg border p-3 text-sm ${diagnostic.severity === "error" ? "border-rust-800 bg-rust-950/30 text-rust-200" : "border-copper-900/70 bg-copper-950/20 text-copper-200"}`}
            >
              <span className="font-mono text-xs">{diagnostic.code}</span>
              <span className="mx-2 text-limestone-500">{diagnostic.path || "/"}</span>
              <span>{diagnostic.message}</span>
            </div>
          ))}
        </div>
      ) : null}
      {stepError ? (
        <div role="alert" className="surface p-4 text-sm text-rust-200">
          {stepError} Open Flow JSON to correct the document without losing its contents.
        </div>
      ) : null}

      {view === "steps" ? (
        <div className="space-y-4">
          <section className="surface grid gap-4 p-4 sm:grid-cols-2">
            <div>
              <label className="field-label" htmlFor="flow-entry-step">Entry step ID</label>
              <input
                id="flow-entry-step"
                className="field-control font-mono"
                value={readText(flow.entry_step_id)}
                onChange={(event) => updateRoot("entry_step_id", event.currentTarget.value)}
                placeholder="Leave empty until an entry is chosen"
              />
            </div>
            <div>
              <label className="field-label" htmlFor="flow-exit-step">Exit step ID</label>
              <input
                id="flow-exit-step"
                className="field-control font-mono"
                value={readText(flow.exit_step_id)}
                onChange={(event) => updateRoot("exit_step_id", event.currentTarget.value)}
                placeholder="Leave empty until an exit is chosen"
              />
            </div>
            <p className="text-xs leading-5 text-limestone-500 sm:col-span-2">
              IDs follow the flow schema. Captures, prerequisites, messages, transitions, and behavior settings are available in Flow JSON.
            </p>
          </section>

          {steps.map((step, index) => (
            <StepEditorCard
              key={`${readText(step.id, `step-${index + 1}`)}-${index}`}
              step={step}
              index={index}
              onUpdate={(updates) => updateStep(index, updates)}
              onRemove={() => onChange(removeFlowStep(flow, index))}
            />
          ))}
          {steps.length === 0 && !stepError ? (
            <div className="surface p-5 text-sm text-limestone-400">
              This flow has no steps. An empty flow can be saved as a draft, but it cannot be published until it is complete.
            </div>
          ) : null}
          <button type="button" className="button-secondary" onClick={addStep} disabled={steps.length >= 128 || Boolean(stepError)}>
            <Plus className="h-4 w-4" />Add step
          </button>
        </div>
      ) : null}

      {view === "flow-map" ? (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(16rem,0.8fr)]">
          <section className="space-y-3" aria-labelledby="flow-nodes-heading">
            <h3 id="flow-nodes-heading" className="text-sm font-semibold text-limestone-200">Steps ({graph.nodes.length})</h3>
            {graph.nodes.length === 0 ? (
              <div className="surface p-5 text-sm text-limestone-400">No graph nodes are defined yet.</div>
            ) : (
              <ol className="space-y-3">
                {graph.nodes.map((node, index) => (
                  <li key={`${node.key}-${index}`}>
                    <div className="surface flex flex-wrap items-center justify-between gap-3 p-4">
                      <div>
                        <p className="font-medium text-limestone-100">{node.title}</p>
                        <p className="mt-1 font-mono text-xs text-limestone-500">{node.id ?? "Missing ID"} · {node.type ?? "Missing type"}</p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {node.isEntry ? <span className="rounded-md border border-moss-700 px-2 py-1 text-xs text-moss-200">Entry</span> : null}
                        {node.isExit ? <span className="rounded-md border border-copper-700 px-2 py-1 text-xs text-copper-200">Exit</span> : null}
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </section>
          <section className="space-y-3" aria-labelledby="flow-edges-heading">
            <h3 id="flow-edges-heading" className="text-sm font-semibold text-limestone-200">Transitions ({graph.edges.length})</h3>
            {graph.edges.length === 0 ? (
              <div className="surface p-5 text-sm text-limestone-400">No transitions are defined. Add transition rules in Flow JSON.</div>
            ) : (
              <ul className="surface divide-y divide-ink-800">
                {graph.edges.map((edge, index) => (
                  <li key={`${edge.fromId}-${edge.toId ?? "missing"}-${index}`} className="p-4 text-sm">
                    <p className="font-mono text-xs text-copper-200">{edge.fromId} → {edge.toId ?? "No target"}</p>
                    <p className="mt-1 text-limestone-400">{edge.label}</p>
                    {!edge.targetExists ? <p className="mt-1 text-xs text-rust-300">Target is not defined in this flow.</p> : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      ) : null}

      {view === "flow-json" ? (
        <div className="surface space-y-3 p-4 sm:p-5">
          <p className="text-sm text-limestone-400">This is the authoring flow object. It uses schema version 1 and is preserved as JSON, including advanced captures, conditions, and messages.</p>
          <label htmlFor="flow-json-editor" className="field-label">Flow document</label>
          <textarea
            id="flow-json-editor"
            className="field-control min-h-[28rem] font-mono text-xs leading-5"
            spellCheck={false}
            value={rawJson}
            onChange={(event) => setRawJson(event.currentTarget.value)}
            aria-describedby={rawError ? "flow-json-error" : undefined}
          />
          {rawError ? <p id="flow-json-error" role="alert" className="text-sm text-rust-200">{rawError}</p> : null}
          <div className="flex flex-wrap items-center gap-3">
            <button type="button" className="button-primary" onClick={applyJson}>Apply flow JSON</button>
            <p className="text-xs text-limestone-500">Applying keeps schema errors visible in the editor; save and validation use the platform contract.</p>
          </div>
        </div>
      ) : null}
    </section>
  );
}

interface StepEditorCardProps {
  step: FlowStep;
  index: number;
  onUpdate: (updates: Record<string, JsonValue>) => void;
  onRemove: () => void;
}

function StepEditorCard({ step, index, onUpdate, onRemove }: StepEditorCardProps) {
  const type = readText(step.type);
  const isEnd = type === "end";
  return (
    <article aria-labelledby={`step-heading-${index}`} className="surface space-y-4 p-4 sm:p-5">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-ink-800 pb-3">
        <div>
          <h3 id={`step-heading-${index}`} className="font-semibold text-limestone-100">Step {index + 1}: {readText(step.title, "Untitled step") || "Untitled step"}</h3>
          <p className="mt-1 font-mono text-xs text-limestone-500">{readText(step.id) || "Missing ID"} · {type || "Missing type"}</p>
        </div>
        <button type="button" className="button-secondary px-3 text-rust-300" onClick={onRemove} aria-label={`Remove step ${readText(step.title, String(index + 1))}`}>
          <Trash2 className="h-4 w-4" />Remove
        </button>
      </header>
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className="field-label" htmlFor={`step-id-${index}`}>Step ID</label>
          <input
            id={`step-id-${index}`}
            className="field-control font-mono"
            value={readText(step.id)}
            onChange={(event) => onUpdate({ id: event.currentTarget.value })}
            autoComplete="off"
          />
        </div>
        <div>
          <label className="field-label" htmlFor={`step-title-${index}`}>Title</label>
          <input
            id={`step-title-${index}`}
            className="field-control"
            value={readText(step.title)}
            onChange={(event) => onUpdate({ title: event.currentTarget.value })}
          />
        </div>
        <div>
          <label className="field-label" htmlFor={`step-type-${index}`}>Type</label>
          <select
            id={`step-type-${index}`}
            className="field-control"
            value={STEP_TYPES.includes(type as typeof STEP_TYPES[number]) ? type : ""}
            onChange={(event) => onUpdate(event.currentTarget.value === "end"
              ? { type: event.currentTarget.value, required: false }
              : { type: event.currentTarget.value })}
          >
            <option value="">Choose a step type</option>
            {STEP_TYPES.map((stepType) => <option key={stepType} value={stepType}>{stepType}</option>)}
          </select>
        </div>
        <div className="flex items-end pb-2">
          <label className="flex min-h-11 items-center gap-2 text-sm text-limestone-300">
            <input
              type="checkbox"
              className="h-4 w-4 accent-copper-500"
              checked={isEnd ? false : step.required !== false}
              disabled={isEnd}
              onChange={(event) => onUpdate({ required: event.currentTarget.checked })}
            />
            Required step
          </label>
        </div>
      </div>
      <div>
        <label className="field-label" htmlFor={`step-instruction-${index}`}>Instruction</label>
        <textarea
          id={`step-instruction-${index}`}
          className="field-control min-h-28"
          value={readText(step.instruction)}
          onChange={(event) => onUpdate({ instruction: event.currentTarget.value })}
          placeholder="Tell the agent what to do at this step."
        />
      </div>
      <div className="max-w-xs">
        <label className="field-label" htmlFor={`step-priority-${index}`}>Priority</label>
        <input
          id={`step-priority-${index}`}
          className="field-control"
          type="number"
          inputMode="numeric"
          min={0}
          max={10000}
          step={1}
          value={typeof step.priority === "number" ? step.priority : 100}
          onChange={(event) => {
            const value = event.currentTarget.valueAsNumber;
            if (Number.isInteger(value) && value >= 0 && value <= 10000) onUpdate({ priority: value });
          }}
        />
      </div>
    </article>
  );
}
