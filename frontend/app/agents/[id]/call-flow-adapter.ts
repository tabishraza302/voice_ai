import { ApiError, api } from "@/lib/api";
import type { AgentStep, AnswerType, BranchCondition, JsonValue, StepBranch, StepInput } from "@/lib/types";

export interface CallFlowAnswer {
  id: string;
  label: string;
  to: string; // "next" | "end" | target_step_id
}

export interface CallFlowStep {
  id: string;
  title: string;
  say: string; // mapped to step.objective
  answers: CallFlowAnswer[];
  other: string; // "next" | "end" | target_step_id
  rawStep?: AgentStep;
}

export function generateFieldKey(title: string, index: number): string {
  const slug = title
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return slug ? `${slug}_${index + 1}` : `step_${index + 1}`;
}

export function agentStepsToCallFlow(steps: AgentStep[]): {
  flowSteps: CallFlowStep[];
  outcomes: AgentStep[];
} {
  const sorted = [...steps].sort((a, b) => a.sort_order - b.sort_order);
  const flowSteps: CallFlowStep[] = [];
  const outcomes: AgentStep[] = [];

  for (const step of sorted) {
    if (step.answer_type === "outcome") {
      outcomes.push(step);
      continue;
    }

    const answers: CallFlowAnswer[] = [];
    let other = "next";

    for (let i = 0; i < step.branches.length; i++) {
      const b = step.branches[i];
      const isConditional = b.conditions && b.conditions.length > 0;
      let target = "next";

      if (b.target_step_id) {
        target = b.target_step_id;
      } else if (b.outcome) {
        target = "end";
      }

      if (isConditional) {
        const label = String(b.conditions[0]?.operand ?? "");
        answers.push({
          id: `ans_${step.id}_${i}`,
          label,
          to: target,
        });
      } else {
        // Fallback / otherwise branch
        other = target;
      }
    }

    flowSteps.push({
      id: step.id,
      title: step.title || "",
      say: step.objective || "",
      answers,
      other,
      rawStep: step,
    });
  }

  return { flowSteps, outcomes };
}

export function resolveCallFlowRoute(target: string, index: number, steps: CallFlowStep[]): string {
  return target === "next" ? steps[index + 1]?.id ?? "end" : target;
}

/** Raw branch index encoded in an answer id (`ans_${step.id}_${branchIndex}`), if any. */
function answerBranchIndex(stepId: string, answerId: string): number | null {
  const prefix = `ans_${stepId}_`;
  if (!answerId.startsWith(prefix)) return null;
  const suffix = answerId.slice(prefix.length);
  if (!/^\d+$/.test(suffix)) return null;
  return Number(suffix);
}

/** Synthesized condition operands follow the step's answer type. */
function coerceOperand(label: string, answerType: AnswerType | null): JsonValue {
  if (answerType === "boolean") {
    if (label === "true") return true;
    if (label === "false") return false;
    return label;
  }
  if (answerType === "number") {
    const value = Number(label);
    return label !== "" && Number.isFinite(value) ? value : label;
  }
  return label;
}

export function callFlowToStepInput(
  flowStep: CallFlowStep,
  allFlowSteps: CallFlowStep[],
  index: number,
  outcomeFieldKeys: { end?: string } = {}
): StepInput {
  const raw = flowStep.rawStep;
  const fieldKey = raw?.field_key || generateFieldKey(flowStep.title, index);
  const endKey = outcomeFieldKeys.end || "end_call";
  const answerType = raw?.answer_type || (flowStep.answers.length > 0 ? "enum" : "text");

  const nextStep = allFlowSteps[index + 1];

  const branches: StepBranch[] = [];

  // Conditional answers
  for (const a of flowStep.answers) {
    const branchIndex = answerBranchIndex(flowStep.id, a.id);
    const rawBranch = branchIndex !== null ? raw?.branches[branchIndex] : undefined;
    let conditions: BranchCondition[];

    if (rawBranch && rawBranch.conditions.length > 1) {
      // Multi-condition branches are reused verbatim; only the target may change.
      conditions = rawBranch.conditions;
    } else if (
      rawBranch &&
      rawBranch.conditions.length === 1 &&
      String(rawBranch.conditions[0].operand) === a.label
    ) {
      // Label untouched: keep the original operator and operand type.
      conditions = [rawBranch.conditions[0]];
    } else {
      conditions = [
        {
          field_key: fieldKey,
          operator: "eq",
          operand: coerceOperand(a.label.trim(), answerType),
        },
      ];
    }

    if (a.to === "next") {
      if (nextStep) {
        branches.push({ conditions, target_step_id: nextStep.id, outcome: null });
      } else {
        branches.push({ conditions, target_step_id: null, outcome: endKey });
      }
    } else if (a.to === "end") {
      branches.push({ conditions, target_step_id: null, outcome: endKey });
    } else {
      branches.push({ conditions, target_step_id: a.to, outcome: null });
    }
  }

  // Fallback "other" route
  if (flowStep.other !== "next") {
    if (flowStep.other === "end") {
      branches.push({ conditions: [], target_step_id: null, outcome: endKey });
    } else {
      branches.push({ conditions: [], target_step_id: flowStep.other, outcome: null });
    }
  }

  const answerLabels = flowStep.answers.map((a) => a.label.trim()).filter(Boolean);

  return {
    title: flowStep.title.trim() || `Step ${index + 1}`,
    objective: flowStep.say.trim() || "Follow conversation flow.",
    field_key: fieldKey,
    answer_type: answerType,
    required: raw?.required ?? true,
    enabled: raw?.enabled ?? true,
    enum_values:
      answerType === "enum"
        ? [...new Set([...(raw?.enum_values ?? []), ...answerLabels])]
        : raw?.enum_values ?? [],
    wording_mode: raw?.wording_mode || "flexible",
    localized_phrases: raw?.localized_phrases || {},
    extraction_instructions:
      raw?.extraction_instructions ||
      (flowStep.answers.length > 0
        ? "Extract the caller's answer from the supported options."
        : "Extract the caller's response."),
    clarification_instructions:
      raw?.clarification_instructions || "Ask the caller to clarify their response.",
    validation_rules: raw?.validation_rules || [],
    branches,
    validation_failure_target: raw?.validation_failure_target || null,
  };
}

/**
 * Ensures that if any flow step routes to 'end',
 * the corresponding enabled outcome step exists in the backend.
 * Auto-provisions it if missing to satisfy backend validate_publish constraints.
 */
export async function ensureOutcomeSteps(
  agentId: string,
  currentSteps: AgentStep[],
  referencedOutcomes: Set<string>
): Promise<{
  steps: AgentStep[];
  outcomeKeys: { end?: string };
}> {
  let updatedSteps = [...currentSteps];
  const outcomeKeys: { end?: string } = {};

  const existingOutcomes = updatedSteps.filter(
    (s) => s.answer_type === "outcome" && s.enabled && typeof s.field_key === "string"
  );

  const existingEnd = existingOutcomes.find((s) =>
    ["end_call", "end", "completed", "finish"].includes(s.field_key || "")
  );
  if (existingEnd) {
    outcomeKeys.end = existingEnd.field_key!;
  }

  // Auto-provision "end" outcome if referenced and not present
  if (referencedOutcomes.has("end") && !outcomeKeys.end) {
    try {
      const endStep = await api<AgentStep>(`/agents/${agentId}/steps`, {
        method: "POST",
        body: JSON.stringify({
          title: "End call",
          objective: "End the call gracefully.",
          field_key: "end_call",
          answer_type: "outcome",
          enabled: true,
        }),
      });
      updatedSteps.push(endStep);
      outcomeKeys.end = endStep.field_key || "end_call";
    } catch (reason) {
      if (!(reason instanceof ApiError) || reason.status !== 409) throw reason;
      // A previous or concurrent save already created the outcome; adopt it.
      const serverSteps = await api<AgentStep[]>(`/agents/${agentId}/steps`);
      const serverEnd = serverSteps.find(
        (step) => step.answer_type === "outcome" && step.field_key === "end_call"
      );
      if (serverEnd) updatedSteps.push(serverEnd);
      outcomeKeys.end = "end_call";
    }
  }

  return { steps: updatedSteps, outcomeKeys };
}

export function getSampleHearingScript(): CallFlowStep[] {
  return [
    {
      id: "sample_s1",
      title: "Greeting",
      say: "Namaste, this is Asha from HearHelp. Am I speaking with {{name}}? Do you have two minutes for a quick follow-up on your hearing check?",
      answers: [
        { id: "sample_a1", label: "Yes, go ahead", to: "next" },
        { id: "sample_a2", label: "Busy right now", to: "sample_s6" },
      ],
      other: "sample_s6",
    },
    {
      id: "sample_s2",
      title: "Recap the result",
      say: "You took our online hearing check last week. It suggested some difficulty with softer, high-pitched sounds. Have you noticed that yourself?",
      answers: [
        { id: "sample_a3", label: "Yes, I have", to: "next" },
        { id: "sample_a4", label: "Not really", to: "sample_s4" },
      ],
      other: "next",
    },
    {
      id: "sample_s3",
      title: "Everyday impact",
      say: "Where does it bother you most: noisy rooms, the TV, or phone calls?",
      answers: [
        { id: "sample_a5", label: "Any of those", to: "next" },
        { id: "sample_a6", label: "It doesn't bother me", to: "end" },
      ],
      other: "next",
    },
    {
      id: "sample_s4",
      title: "Offer a clinic test",
      say: "We can book a free hearing test with an audiologist at a partner clinic near you. Shall I find you a slot?",
      answers: [
        { id: "sample_a7", label: "Yes, book it", to: "next" },
        { id: "sample_a8", label: "How much does it cost?", to: "sample_s7" },
        { id: "sample_a9", label: "Not now", to: "sample_s6" },
      ],
      other: "end",
    },
    {
      id: "sample_s5",
      title: "Book a slot",
      say: "Great. Do weekdays or weekends suit you better? I'll send the clinic details by SMS.",
      answers: [
        { id: "sample_a10", label: "Weekdays", to: "end" },
        { id: "sample_a11", label: "Weekends", to: "end" },
      ],
      other: "end",
    },
    {
      id: "sample_s6",
      title: "Arrange a callback",
      say: "No problem. Would morning or evening be better for a call back?",
      answers: [{ id: "sample_a12", label: "Gives a time", to: "end" }],
      other: "end",
    },
    {
      id: "sample_s7",
      title: "Answer the cost question",
      say: "The first screening at the partner clinic is free. Anything beyond that is discussed only after the test, and it's your decision.",
      answers: [{ id: "sample_a13", label: "Okay, that's fine", to: "sample_s4" }],
      other: "end",
    },
  ];
}
