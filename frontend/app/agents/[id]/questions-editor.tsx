"use client";

import { useEffect, useMemo, useState } from "react";
import { ApiError, api } from "@/lib/api";
import type { AgentStep, AnswerType, JsonValue, StepBranch, StepInput, ValidationOperator, ValidationRule } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

const answerTypes: Array<{ value: AnswerType; label: string }> = [
  { value: "boolean", label: "Yes / no" },
  { value: "text", label: "Text" },
  { value: "number", label: "Number" },
  { value: "currency", label: "Currency" },
  { value: "enum", label: "Choice" },
  { value: "message", label: "Information message" },
  { value: "outcome", label: "Outcome" },
];

const operatorLabels: Record<ValidationOperator, string> = {
  eq: "is equal to",
  ne: "is not equal to",
  gt: "is greater than",
  gte: "is at least",
  lt: "is less than",
  lte: "is at most",
  in: "is one of",
};

const allOperators: ValidationOperator[] = ["eq", "ne", "gt", "gte", "lt", "lte", "in"];
const setOperators: ValidationOperator[] = ["eq", "ne", "in"];

function inputFromStep(step: AgentStep): StepInput {
  return {
    field_key: step.field_key,
    title: step.title,
    objective: step.objective,
    required: step.required,
    enabled: step.enabled,
    answer_type: step.answer_type,
    enum_values: [...step.enum_values],
    wording_mode: step.wording_mode,
    localized_phrases: { ...step.localized_phrases },
    extraction_instructions: step.extraction_instructions,
    clarification_instructions: step.clarification_instructions,
    validation_rules: step.validation_rules.map((rule) => ({ ...rule })),
    branches: step.branches.map((branch) => ({
      ...branch,
      conditions: branch.conditions.map((condition) => ({ ...condition })),
    })),
    validation_failure_target: step.validation_failure_target ? { ...step.validation_failure_target } : null,
  };
}

function valueForType(type: AnswerType | null): JsonValue {
  if (type === "boolean") return false;
  if (type === "currency") return "0.00";
  if (type === "number") return 0;
  if (type === "enum") return "";
  return "";
}

function failureText(reason: unknown, fallback: string): string {
  if (!(reason instanceof ApiError)) return fallback;
  return reason.status > 0 ? `${reason.message} (HTTP ${reason.status})` : reason.message;
}

function OperandInput({
  id,
  label,
  answerType,
  operator,
  enumValues,
  value,
  onChange,
}: {
  id: string;
  label: string;
  answerType: AnswerType | null;
  operator: ValidationOperator | null;
  enumValues: string[];
  value: JsonValue;
  onChange: (value: JsonValue) => void;
}) {
  const displayValue = Array.isArray(value)
    ? value.map((item) => typeof item === "string" ? item : String(item)).join(", ")
    : value === null ? "" : String(value);

  function change(raw: string) {
    const convert = (item: string): JsonValue => {
      if (answerType === "boolean") return item === "true";
      if (answerType === "number") {
        const number = Number(item);
        return item.trim() && Number.isFinite(number) ? number : item;
      }
      return item;
    };
    if (operator === "in") {
      onChange(raw.split(",").map((item) => item.trim()).filter(Boolean).map(convert));
    } else if (raw === "" && answerType === "boolean") {
      onChange(null);
    } else {
      onChange(convert(raw));
    }
  }

  if (answerType === "boolean" && operator === "in") {
    const selectedValues = Array.isArray(value) ? value.filter((item): item is boolean => typeof item === "boolean") : [];
    return (
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">{label}</legend>
        {[true, false].map((option) => (
          <label key={String(option)} className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              className="size-4 rounded border-input accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              checked={selectedValues.includes(option)}
              onChange={(event) => onChange(event.currentTarget.checked
                ? [...selectedValues, option]
                : selectedValues.filter((item) => item !== option))}
            />
            {option ? "Yes" : "No"}
          </label>
        ))}
      </fieldset>
    );
  }

  if (answerType === "boolean") {
    return (
      <div className="space-y-1.5">
        <Label htmlFor={id}>{label}</Label>
        <Select id={id} value={displayValue} onChange={(event) => change(event.currentTarget.value)}>
          <option value="">Choose a value</option>
          <option value="true">Yes</option>
          <option value="false">No</option>
        </Select>
      </div>
    );
  }

  if (answerType === "enum" && operator !== "in" && enumValues.length > 0) {
    return (
      <div className="space-y-1.5">
        <Label htmlFor={id}>{label}</Label>
        <Select id={id} value={displayValue} onChange={(event) => change(event.currentTarget.value)}>
          <option value="">Choose a value</option>
          {enumValues.map((option) => <option key={option} value={option}>{option}</option>)}
        </Select>
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type={answerType === "number" && operator !== "in" ? "number" : "text"}
        step={answerType === "number" ? "any" : undefined}
        inputMode={answerType === "currency" || answerType === "number" ? "decimal" : undefined}
        value={displayValue}
        onChange={(event) => change(event.currentTarget.value)}
        aria-describedby={answerType === "currency" ? `${id}-help` : undefined}
      />
      {answerType === "currency" ? <p id={`${id}-help`} className="text-xs text-muted-foreground">Enter a decimal amount, for example 1250.00.</p> : null}
      {operator === "in" ? <p className="text-xs text-muted-foreground">Separate values with commas.</p> : null}
    </div>
  );
}

function TargetSelect({
  id,
  label,
  value,
  steps,
  outcomes,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  steps: AgentStep[];
  outcomes: Array<AgentStep & { field_key: string }>;
  onChange: (stepId: string | null, outcome: string | null) => void;
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Select
        id={id}
        value={value}
        onChange={(event) => {
          const selected = event.currentTarget.value;
          if (selected.startsWith("step:")) onChange(selected.slice(5), null);
          else if (selected.startsWith("outcome:")) onChange(null, selected.slice(8));
          else onChange(null, null);
        }}
      >
        <option value="">Choose a destination</option>
        {steps.map((step) => <option key={`step-${step.id}`} value={`step:${step.id}`}>Step: {step.title || step.field_key || "Untitled"}</option>)}
        {outcomes.map((step) => <option key={`outcome-${step.id}`} value={`outcome:${step.field_key}`}>Outcome: {step.title || step.field_key}</option>)}
      </Select>
    </div>
  );
}

export function QuestionsEditor({
  agentId,
  languages,
  steps,
  published,
  onStepsChange,
}: {
  agentId: string;
  languages: string[];
  steps: AgentStep[];
  published: boolean;
  onStepsChange: (steps: AgentStep[]) => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(steps[0]?.id ?? null);
  const activeStep = steps.find((step) => step.id === selectedId) ?? steps[0] ?? null;
  const [draft, setDraft] = useState<StepInput | null>(activeStep ? inputFromStep(activeStep) : null);
  const [error, setError] = useState<string | null>(null);
  const [savedMessage, setSavedMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [reordering, setReordering] = useState(false);

  useEffect(() => {
    if (!steps.some((step) => step.id === selectedId)) setSelectedId(steps[0]?.id ?? null);
  }, [selectedId, steps]);

  useEffect(() => {
    setDraft(activeStep ? inputFromStep(activeStep) : null);
    setError(null);
    setSavedMessage(null);
  }, [activeStep?.id]);

  const dirty = useMemo(() => {
    if (!activeStep || !draft) return false;
    return JSON.stringify(inputFromStep(activeStep)) !== JSON.stringify(draft);
  }, [activeStep, draft]);

  const routeSteps = steps.filter((step) => step.enabled && step.id !== activeStep?.id);
  const outcomes = steps.filter((step): step is AgentStep & { field_key: string } => step.enabled && step.answer_type === "outcome" && step.field_key !== null);
  const conditionFields = steps.filter((step): step is AgentStep & { field_key: string; answer_type: Exclude<AnswerType, "message" | "outcome"> } =>
    step.enabled && step.field_key !== null && step.answer_type !== null && ["boolean", "text", "number", "currency", "enum"].includes(step.answer_type),
  );
  const ruleOperators = draft?.answer_type === "boolean" || draft?.answer_type === "text" || draft?.answer_type === "enum" ? setOperators : allOperators;

  function updateDraft<K extends keyof StepInput>(key: K, value: StepInput[K]) {
    setDraft((current) => current ? { ...current, [key]: value } : current);
    setError(null);
    setSavedMessage(null);
  }

  async function addStep() {
    setError(null);
    setSaving(true);
    let suffix = steps.length + 1;
    while (steps.some((step) => step.field_key === `question_${suffix}`)) suffix += 1;
    const fieldKey = `question_${suffix}`;
    try {
      const created = await api<AgentStep>(`/agents/${agentId}/steps`, {
        method: "POST",
        body: JSON.stringify({
          field_key: fieldKey,
          title: published ? "New question" : "",
          objective: "",
          required: true,
          // A published agent must stay valid after every step change. A new
          // step starts disabled until the owner has configured its required
          // instructions and explicitly enables it.
          enabled: !published,
          answer_type: "text",
          enum_values: [],
          wording_mode: "flexible",
          localized_phrases: {},
          extraction_instructions: "",
          clarification_instructions: "",
          validation_rules: [],
          branches: [],
          validation_failure_target: null,
        }),
      });
      onStepsChange([...steps, created]);
      setSelectedId(created.id);
    } catch (reason) {
      setError(failureText(reason, "Could not add this question. Try again."));
    } finally {
      setSaving(false);
    }
  }

  async function saveStep() {
    if (!activeStep || !draft) return;
    setError(null);
    setSavedMessage(null);
    setSaving(true);
    try {
      const updated = await api<AgentStep>(`/agents/${agentId}/steps/${activeStep.id}`, {
        method: "PATCH",
        body: JSON.stringify(draft),
      });
      onStepsChange(steps.map((step) => step.id === updated.id ? updated : step));
      setDraft(inputFromStep(updated));
      setSavedMessage("Question saved.");
    } catch (reason) {
      setError(failureText(reason, "Could not save this question. Try again."));
    } finally {
      setSaving(false);
    }
  }

  async function removeStep() {
    if (!activeStep) return;
    setError(null);
    setDeleting(true);
    try {
      await api<void>(`/agents/${agentId}/steps/${activeStep.id}`, { method: "DELETE" });
      const remaining = steps.filter((step) => step.id !== activeStep.id);
      onStepsChange(remaining);
      setSelectedId(remaining[0]?.id ?? null);
      setDeleteOpen(false);
    } catch (reason) {
      setError(failureText(reason, "Could not delete this question. Try again."));
    } finally {
      setDeleting(false);
    }
  }

  async function moveStep(index: number, direction: -1 | 1) {
    const nextIndex = index + direction;
    if (nextIndex < 0 || nextIndex >= steps.length) return;
    const reordered = [...steps];
    const [item] = reordered.splice(index, 1);
    reordered.splice(nextIndex, 0, item);
    setError(null);
    setReordering(true);
    try {
      const result = await api<AgentStep[]>(`/agents/${agentId}/steps/order`, {
        method: "PUT",
        body: JSON.stringify({ step_ids: reordered.map((step) => step.id) }),
      });
      onStepsChange(result);
    } catch (reason) {
      setError(failureText(reason, "Could not reorder the questions. Try again."));
    } finally {
      setReordering(false);
    }
  }

  function updateRule(index: number, rule: ValidationRule) {
    if (!draft) return;
    const rules = draft.validation_rules.map((item, itemIndex) => itemIndex === index ? rule : item);
    updateDraft("validation_rules", rules);
  }

  function updateBranch(index: number, branch: StepBranch) {
    if (!draft) return;
    updateDraft("branches", draft.branches.map((item, itemIndex) => itemIndex === index ? branch : item));
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[15rem_minmax(0,1fr)]">
      <aside className="space-y-4">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold">Question order</h3>
          <Button size="sm" onClick={() => void addStep()} disabled={saving || reordering}>Add question</Button>
        </div>
        {published ? (
          <p className="text-xs leading-5 text-muted-foreground">New questions start disabled so this published agent remains callable. Configure the question, then turn on Enabled when it is ready.</p>
        ) : null}
        {steps.length === 0 ? (
          <p className="rounded-xl border border-dashed p-4 text-sm leading-6 text-muted-foreground">No questions yet. Add a step to start the call flow.</p>
        ) : (
          <ol className="space-y-2">
            {steps.map((step, index) => (
              <li key={step.id} className="rounded-xl border bg-card p-3">
                <button
                  type="button"
                  className="w-full rounded-md text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  aria-current={activeStep?.id === step.id ? "step" : undefined}
                  onClick={() => setSelectedId(step.id)}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate text-sm font-medium">{step.title || step.field_key || "Untitled question"}</span>
                    <Badge variant={step.enabled ? "muted" : "warning"}>{step.enabled ? "On" : "Off"}</Badge>
                  </span>
                  <span className="mt-1 block text-xs text-muted-foreground">Step {index + 1} · {step.answer_type ?? "Not set"}</span>
                </button>
                <div className="mt-2 flex gap-1">
                  <Button variant="ghost" size="sm" aria-label={`Move ${step.title || "question"} up`} disabled={index === 0 || reordering} onClick={() => void moveStep(index, -1)}>Move up</Button>
                  <Button variant="ghost" size="sm" aria-label={`Move ${step.title || "question"} down`} disabled={index === steps.length - 1 || reordering} onClick={() => void moveStep(index, 1)}>Move down</Button>
                </div>
              </li>
            ))}
          </ol>
        )}
        {error ? <p role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-sm text-destructive">{error}</p> : null}
      </aside>

      {activeStep && draft ? (
        <section className="min-w-0 space-y-6" aria-labelledby="question-editor-heading">
          <div className="flex flex-wrap items-start justify-between gap-3 border-b pb-4">
            <div>
              <p className="text-sm text-muted-foreground">Step {steps.findIndex((step) => step.id === activeStep.id) + 1}</p>
              <h3 id="question-editor-heading" className="mt-1 text-lg font-semibold">Edit question</h3>
            </div>
            <Button variant="destructive" size="sm" onClick={() => setDeleteOpen(true)} disabled={saving || deleting}>Delete question</Button>
          </div>

          <Card className="space-y-5 p-4 sm:p-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="step-title">Question title</Label>
                <Input id="step-title" maxLength={255} value={draft.title} onChange={(event) => updateDraft("title", event.currentTarget.value)} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="step-field-key">Field key</Label>
                <Input id="step-field-key" maxLength={128} value={draft.field_key ?? ""} onChange={(event) => updateDraft("field_key", event.currentTarget.value.trim() ? event.currentTarget.value : null)} aria-describedby="field-key-help" />
                <p id="field-key-help" className="text-xs text-muted-foreground">Unique within this agent. Used in validation and routing.</p>
              </div>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="step-objective">Objective</Label>
              <Textarea id="step-objective" value={draft.objective} onChange={(event) => updateDraft("objective", event.currentTarget.value)} />
              <p className="text-xs text-muted-foreground">Explain what this step needs to collect or communicate.</p>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="step-answer-type">Answer type</Label>
                <Select
                  id="step-answer-type"
                  value={draft.answer_type ?? ""}
                  onChange={(event) => {
                    const selected = answerTypes.find((option) => option.value === event.currentTarget.value)?.value ?? null;
                    if (!selected) return;
                    setDraft((current) => current ? {
                      ...current,
                      answer_type: selected,
                      enum_values: selected === "enum" ? current.enum_values : [],
                      validation_rules: selected === "message" || selected === "outcome" ? [] : current.validation_rules,
                      validation_failure_target: selected === "message" || selected === "outcome" ? null : current.validation_failure_target,
                      branches: selected === "outcome" ? [] : current.branches,
                      wording_mode: selected === "outcome" ? null : current.wording_mode ?? "flexible",
                      localized_phrases: selected === "outcome" ? {} : current.localized_phrases,
                    } : current);
                    setError(null);
                    setSavedMessage(null);
                  }}
                >
                  <option value="">Choose an answer type</option>
                  {answerTypes.map((type) => <option key={type.value} value={type.value}>{type.label}</option>)}
                </Select>
              </div>
              <div className="flex flex-wrap gap-5 self-end pb-2">
                <label className="inline-flex items-center gap-2 text-sm">
                  <input type="checkbox" className="size-4 rounded border-input accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" checked={draft.required} onChange={(event) => updateDraft("required", event.currentTarget.checked)} />
                  Required
                </label>
                <label className="inline-flex items-center gap-2 text-sm">
                  <input type="checkbox" className="size-4 rounded border-input accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" checked={draft.enabled} onChange={(event) => updateDraft("enabled", event.currentTarget.checked)} />
                  Enabled
                </label>
              </div>
            </div>

            {draft.answer_type === "enum" ? (
              <div className="space-y-1.5">
                <Label htmlFor="enum-values">Choice values</Label>
                <Input id="enum-values" value={draft.enum_values.join(", ")} onChange={(event) => updateDraft("enum_values", event.currentTarget.value.split(",").map((item) => item.trim()).filter(Boolean))} aria-describedby="enum-values-help" />
                <p id="enum-values-help" className="text-xs text-muted-foreground">Separate each option with a comma.</p>
              </div>
            ) : null}

            {draft.answer_type && draft.answer_type !== "outcome" ? (
              <div className="space-y-4 border-t pt-5">
                <div className="max-w-sm space-y-1.5">
                  <Label htmlFor="wording-mode">Wording mode</Label>
                  <Select id="wording-mode" value={draft.wording_mode ?? "flexible"} onChange={(event) => updateDraft("wording_mode", event.currentTarget.value === "exact" ? "exact" : "flexible")}>
                    <option value="flexible">Flexible conversation</option>
                    <option value="exact">Use saved exact wording</option>
                  </Select>
                </div>
                {["boolean", "text", "number", "currency", "enum"].includes(draft.answer_type) ? (
                  <>
                    <div className="space-y-1.5">
                      <Label htmlFor="extraction-instructions">Extraction instructions</Label>
                      <Textarea id="extraction-instructions" value={draft.extraction_instructions} onChange={(event) => updateDraft("extraction_instructions", event.currentTarget.value)} />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="clarification-instructions">Clarification instructions</Label>
                      <Textarea id="clarification-instructions" value={draft.clarification_instructions} onChange={(event) => updateDraft("clarification_instructions", event.currentTarget.value)} />
                    </div>
                  </>
                ) : null}
                {draft.wording_mode === "exact" ? (
                  <div className="space-y-3 rounded-xl border bg-muted/40 p-4">
                    <div>
                      <h4 className="text-sm font-semibold">Exact opening phrases</h4>
                      <p className="mt-1 text-xs leading-5 text-muted-foreground">Add one saved phrase for every enabled language.</p>
                    </div>
                    {languages.length === 0 ? <p className="text-sm text-muted-foreground">Select a language in the Languages tab first.</p> : null}
                    {languages.map((language) => (
                      <div key={language} className="space-y-1.5">
                        <Label htmlFor={`phrase-${language}`}>Phrase ({language})</Label>
                        <Textarea id={`phrase-${language}`} value={draft.localized_phrases[language] ?? ""} onChange={(event) => updateDraft("localized_phrases", { ...draft.localized_phrases, [language]: event.currentTarget.value })} />
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}
          </Card>

          {draft && ["boolean", "text", "number", "currency", "enum"].includes(draft.answer_type ?? "") ? (
            <Card className="space-y-5 p-4 sm:p-5">
              <div>
                <h4 className="font-semibold">Validation rules</h4>
                <p className="mt-1 text-sm leading-5 text-muted-foreground">Rules check the collected value. A failed rule uses the failure destination below.</p>
              </div>
              {draft.validation_rules.length === 0 ? <p className="text-sm text-muted-foreground">No validation rules configured.</p> : null}
              <div className="space-y-4">
                {draft.validation_rules.map((rule, index) => (
                  <div key={`rule-${index}`} className="grid gap-3 rounded-xl border p-3 sm:grid-cols-[minmax(8rem,0.65fr)_minmax(10rem,1fr)_auto] sm:items-end">
                    <div className="space-y-1.5">
                      <Label htmlFor={`rule-op-${index}`}>Operator</Label>
                      <Select
                        id={`rule-op-${index}`}
                        value={rule.operator ?? ""}
                        onChange={(event) => {
                          const selected = ruleOperators.find((operator) => operator === event.currentTarget.value) ?? null;
                          updateRule(index, { ...rule, operator: selected });
                        }}
                      >
                        <option value="">Choose an operator</option>
                        {ruleOperators.map((operator) => <option key={operator} value={operator}>{operatorLabels[operator]}</option>)}
                      </Select>
                    </div>
                    <OperandInput
                      id={`rule-operand-${index}`}
                      label="Expected value"
                      answerType={draft.answer_type}
                      operator={rule.operator}
                      enumValues={draft.enum_values}
                      value={rule.operand}
                      onChange={(operand) => updateRule(index, { ...rule, operand })}
                    />
                    <Button variant="outline" size="sm" aria-label={`Remove validation rule ${index + 1}`} onClick={() => updateDraft("validation_rules", draft.validation_rules.filter((_, itemIndex) => itemIndex !== index))}>Remove</Button>
                  </div>
                ))}
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => updateDraft("validation_rules", [...draft.validation_rules, { operator: "eq", operand: valueForType(draft.answer_type) }])}
              >
                Add validation rule
              </Button>
              {draft.validation_rules.length > 0 ? (
                <TargetSelect
                  id="validation-failure-target"
                  label="If a rule fails"
                  value={draft.validation_failure_target?.target_step_id ? `step:${draft.validation_failure_target.target_step_id}` : draft.validation_failure_target?.outcome ? `outcome:${draft.validation_failure_target.outcome}` : ""}
                  steps={routeSteps}
                  outcomes={outcomes}
                  onChange={(target_step_id, outcome) => updateDraft("validation_failure_target", target_step_id || outcome ? { target_step_id, outcome } : null)}
                />
              ) : null}
            </Card>
          ) : null}

          {draft.answer_type && draft.answer_type !== "outcome" ? (
            <Card className="space-y-5 p-4 sm:p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h4 className="font-semibold">Branching</h4>
                  <p className="mt-1 text-sm leading-5 text-muted-foreground">Rules are checked in order. The first matching branch is taken.</p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => updateDraft("branches", [...draft.branches, { conditions: [{ field_key: null, operator: null, operand: "" }], target_step_id: null, outcome: null }])}
                >
                  Add branch
                </Button>
              </div>
              {draft.branches.length === 0 ? <p className="text-sm text-muted-foreground">No branches configured. The flow continues in question order.</p> : null}
              <div className="space-y-4">
                {draft.branches.map((branch, branchIndex) => (
                  <div key={`branch-${branchIndex}`} className="space-y-4 rounded-xl border p-4">
                    <div className="flex items-center justify-between gap-3">
                      <h5 className="text-sm font-semibold">Branch {branchIndex + 1}</h5>
                      <Button variant="ghost" size="sm" onClick={() => updateDraft("branches", draft.branches.filter((_, itemIndex) => itemIndex !== branchIndex))}>Remove branch</Button>
                    </div>
                    {branch.conditions.map((condition, conditionIndex) => {
                      const referenced = conditionFields.find((step) => step.field_key === condition.field_key);
                      const referencedType = referenced?.answer_type ?? null;
                      const operators = referencedType === "boolean" || referencedType === "text" || referencedType === "enum" ? setOperators : allOperators;
                      return (
                        <div key={`condition-${conditionIndex}`} className="grid gap-3 rounded-lg bg-muted/40 p-3 sm:grid-cols-2">
                          <div className="space-y-1.5">
                            <Label htmlFor={`branch-${branchIndex}-field-${conditionIndex}`}>When field</Label>
                            <Select
                              id={`branch-${branchIndex}-field-${conditionIndex}`}
                              value={condition.field_key ?? ""}
                              onChange={(event) => {
                                const field = conditionFields.find((step) => step.field_key === event.currentTarget.value);
                                const operator: ValidationOperator | null = field ? "eq" : null;
                                const updated = { ...condition, field_key: field?.field_key ?? null, operator, operand: field ? valueForType(field.answer_type) : "" };
                                updateBranch(branchIndex, { ...branch, conditions: branch.conditions.map((item, index) => index === conditionIndex ? updated : item) });
                              }}
                            >
                              <option value="">Choose a question field</option>
                              {conditionFields.map((step) => <option key={step.id} value={step.field_key ?? ""}>{step.title || step.field_key}</option>)}
                            </Select>
                          </div>
                          <div className="space-y-1.5">
                            <Label htmlFor={`branch-${branchIndex}-op-${conditionIndex}`}>Operator</Label>
                            <Select
                              id={`branch-${branchIndex}-op-${conditionIndex}`}
                              value={condition.operator ?? ""}
                              onChange={(event) => {
                                const operator = operators.find((item) => item === event.currentTarget.value) ?? null;
                                updateBranch(branchIndex, { ...branch, conditions: branch.conditions.map((item, index) => index === conditionIndex ? { ...condition, operator } : item) });
                              }}
                              disabled={!referenced}
                            >
                              <option value="">Choose an operator</option>
                              {operators.map((operator) => <option key={operator} value={operator}>{operatorLabels[operator]}</option>)}
                            </Select>
                          </div>
                          {referenced ? (
                            <OperandInput
                              id={`branch-${branchIndex}-operand-${conditionIndex}`}
                              label="Compare with"
                              answerType={referencedType}
                              operator={condition.operator}
                              enumValues={referenced.enum_values}
                              value={condition.operand}
                              onChange={(operand) => updateBranch(branchIndex, { ...branch, conditions: branch.conditions.map((item, index) => index === conditionIndex ? { ...condition, operand } : item) })}
                            />
                          ) : null}
                          <div className="flex items-end gap-2">
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={conditionIndex === 0}
                              onClick={() => {
                                const conditions = [...branch.conditions];
                                const [item] = conditions.splice(conditionIndex, 1);
                                conditions.splice(conditionIndex - 1, 0, item);
                                updateBranch(branchIndex, { ...branch, conditions });
                              }}
                              aria-label={`Move condition ${conditionIndex + 1} up`}
                            >
                              Move up
                            </Button>
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => updateBranch(branchIndex, { ...branch, conditions: branch.conditions.filter((_, index) => index !== conditionIndex) })}
                            >
                              Remove condition
                            </Button>
                          </div>
                        </div>
                      );
                    })}
                    <div className="flex flex-wrap items-end justify-between gap-3">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => updateBranch(branchIndex, { ...branch, conditions: [...branch.conditions, { field_key: null, operator: null, operand: "" }] })}
                      >
                        Add condition
                      </Button>
                      <div className="min-w-60 flex-1">
                        <TargetSelect
                          id={`branch-target-${branchIndex}`}
                          label="Then go to"
                          value={branch.target_step_id ? `step:${branch.target_step_id}` : branch.outcome ? `outcome:${branch.outcome}` : ""}
                          steps={routeSteps}
                          outcomes={outcomes}
                          onChange={(target_step_id, outcome) => updateBranch(branchIndex, { ...branch, target_step_id, outcome })}
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          ) : null}

          <div className="flex flex-col-reverse gap-3 border-t pt-5 sm:flex-row sm:items-center sm:justify-between">
            <div aria-live="polite" className="text-sm">
              {error ? <span className="text-destructive">{error}</span> : savedMessage ? <span className="text-primary">{savedMessage}</span> : null}
            </div>
            <div className="flex gap-2 sm:justify-end">
              <Button variant="outline" onClick={() => setDraft(inputFromStep(activeStep))} disabled={!dirty || saving}>Discard changes</Button>
              <Button onClick={() => void saveStep()} disabled={!dirty || saving}>
                {saving ? "Saving..." : "Save question"}
              </Button>
            </div>
          </div>
          <ConfirmDialog
            open={deleteOpen}
            title="Delete this question?"
            description={`“${activeStep.title || activeStep.field_key || "Untitled question"}” will be removed from this agent's flow. This cannot be undone.`}
            confirmLabel="Delete question"
            pending={deleting}
            onConfirm={() => void removeStep()}
            onOpenChange={setDeleteOpen}
          />
        </section>
      ) : (
        <div className="rounded-xl border border-dashed p-8 text-center">
          <h3 className="font-semibold">No question selected</h3>
          <p className="mt-2 text-sm text-muted-foreground">Add a question or choose one from the list.</p>
        </div>
      )}
    </div>
  );
}
