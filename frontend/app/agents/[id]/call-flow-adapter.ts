import type { AgentStep, BranchCondition, JsonValue, StepBranch, StepInput } from "@/lib/types";

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
} {
  const sorted = [...steps].sort((a, b) => a.sort_order - b.sort_order);
  const flowSteps: CallFlowStep[] = [];

  for (const step of sorted) {

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

  return { flowSteps };
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

function coerceOperand(label: string): JsonValue {
  return label.trim();
}

export function callFlowToStepInput(
  flowStep: CallFlowStep,
  allFlowSteps: CallFlowStep[],
  index: number
): StepInput {
  const raw = flowStep.rawStep;
  const fieldKey = raw?.field_key || generateFieldKey(flowStep.title, index);
  const nextStep = allFlowSteps[index + 1];

  const branches: StepBranch[] = [];

  // Conditional answers
  for (const a of flowStep.answers) {
    const branchIndex = answerBranchIndex(flowStep.id, a.id);
    const rawBranch = branchIndex !== null ? raw?.branches[branchIndex] : undefined;
    let conditions: BranchCondition[];

    if (rawBranch && rawBranch.conditions.length > 1) {
      conditions = rawBranch.conditions;
    } else if (
      rawBranch &&
      rawBranch.conditions.length === 1 &&
      String(rawBranch.conditions[0].operand) === a.label
    ) {
      conditions = [rawBranch.conditions[0]];
    } else {
      conditions = [
        {
          field_key: fieldKey,
          operator: "eq",
          operand: coerceOperand(a.label),
        },
      ];
    }

    if (a.to === "next") {
      if (nextStep) {
        branches.push({ conditions, target_step_id: nextStep.id, outcome: null });
      } else {
        branches.push({ conditions, target_step_id: null, outcome: "end" });
      }
    } else if (a.to === "end") {
      branches.push({ conditions, target_step_id: null, outcome: "end" });
    } else {
      branches.push({ conditions, target_step_id: a.to, outcome: null });
    }
  }

  // Fallback "other" route
  if (flowStep.other !== "next") {
    if (flowStep.other === "end") {
      branches.push({ conditions: [], target_step_id: null, outcome: "end" });
    } else {
      branches.push({ conditions: [], target_step_id: flowStep.other, outcome: null });
    }
  }

  return {
    title: flowStep.title.trim() || `Step ${index + 1}`,
    objective: flowStep.say.trim() || "Follow conversation flow.",
    field_key: fieldKey,
    branches,
  };
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
