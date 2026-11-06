import { ApiError, api } from "@/lib/api";
import type { AgentStep, StepInput } from "@/lib/types";

import {
  callFlowToStepInput,
  ensureOutcomeSteps,
  resolveCallFlowRoute,
} from "./call-flow-adapter";
import type { CallFlowStep } from "./call-flow-adapter";

export interface CallFlowSaveInput {
  agentId: string;
  model: CallFlowStep[]; // current working model (temp ids allowed)
  serverSteps: AgentStep[]; // last known server state
  signatures: Map<string, string>; // server step id -> signature of last written payload
  onIdsMapped: (idMap: Map<string, string>, createdById: Map<string, AgentStep>) => void;
}

export interface CallFlowSaveOutput {
  serverSteps: AgentStep[]; // canonical list after the save
  signatures: Map<string, string>;
}

export interface CallFlowSaveState {
  status: "idle" | "saving" | "saved" | "error";
  message?: string;
}

/** Rewrites a whole working model through the temp-id -> server-id mapping. */
function remapFlowSteps(
  flowSteps: CallFlowStep[],
  idMap: Map<string, string>,
  createdById: Map<string, AgentStep>
): CallFlowStep[] {
  return flowSteps.map((step) => {
    const created = createdById.get(step.id);
    return {
      ...step,
      id: idMap.get(step.id) ?? step.id,
      rawStep: created ?? step.rawStep,
      answers: step.answers.map((answer) => ({
        ...answer,
        to: idMap.get(answer.to) ?? answer.to,
      })),
      other: idMap.get(step.other) ?? step.other,
    };
  });
}

/**
 * Diffs the working model against the last known server state and persists it:
 * provision outcomes, create new steps (remapping ids after each POST so a
 * retry never re-creates), patch changed steps, delete removed steps, then
 * persist the order only when it actually changed.
 */
export async function saveCallFlow(input: CallFlowSaveInput): Promise<CallFlowSaveOutput> {
  const { agentId, serverSteps, signatures, onIdsMapped } = input;
  let remapped = input.model;

  // 1. Server steps that vanished from the model (outcomes are managed
  // separately by ensureOutcomeSteps / the canonical order).
  const modelIds = new Set(remapped.map((step) => step.id));
  const removed = serverSteps.filter(
    (step) => step.answer_type !== "outcome" && !modelIds.has(step.id)
  );
  const removedIds = new Set(removed.map((step) => step.id));
  const stepsAfterRemovals = serverSteps.filter((step) => !removedIds.has(step.id));

  // 2. Referenced outcomes: resolveCallFlowRoute turns a last-step "next" into
  // "end", so implicit ends are counted too.
  const referenced = new Set<string>();
  for (let index = 0; index < remapped.length; index++) {
    const flowStep = remapped[index];
    for (const answer of flowStep.answers) {
      if (resolveCallFlowRoute(answer.to, index, remapped) === "end") referenced.add("end");
    }
    if (resolveCallFlowRoute(flowStep.other, index, remapped) === "end") referenced.add("end");
  }

  // 3. Provision a missing "end" outcome and 4. fail loudly if an enabled one
  // still isn't available.
  const ensured = await ensureOutcomeSteps(agentId, stepsAfterRemovals, referenced);
  if (referenced.has("end")) {
    const endKey = ensured.outcomeKeys.end ?? "end_call";
    const found = ensured.steps.some(
      (step) => step.answer_type === "outcome" && step.enabled && step.field_key === endKey
    );
    if (!found) {
      throw new Error(
        `Routes to "End call" need an enabled outcome step with field key "${endKey}", but none was found. Enable it or change the routes, then save again.`
      );
    }
  }

  // 5. Create new steps (no rawStep) in model order. Remap the local snapshot
  // after each successful POST so a later failure can't re-create anything.
  const idMap = new Map<string, string>();
  const createdById = new Map<string, AgentStep>();
  for (let index = 0; index < remapped.length; index++) {
    const flowStep = remapped[index];
    if (flowStep.rawStep) continue;
    const created = await api<AgentStep>(`/agents/${agentId}/steps`, {
      method: "POST",
      body: JSON.stringify({
        ...callFlowToStepInput(flowStep, remapped, index, ensured.outcomeKeys),
        branches: [],
      }),
    });
    idMap.set(flowStep.id, created.id);
    createdById.set(flowStep.id, created);
    remapped = remapFlowSteps(remapped, idMap, createdById);
    onIdsMapped(idMap, createdById);
  }

  // 6. PATCH steps whose serialized payload changed; drop validation failure
  // targets that point at nothing that will exist after the save.
  const validIds = new Set(remapped.map((step) => step.id));
  for (const step of ensured.steps) {
    if (step.answer_type === "outcome") validIds.add(step.id);
  }
  const updatedById = new Map<string, AgentStep>();
  for (let index = 0; index < remapped.length; index++) {
    const flowStep = remapped[index];
    const payload: StepInput = callFlowToStepInput(flowStep, remapped, index, ensured.outcomeKeys);
    const failureTarget = payload.validation_failure_target;
    if (failureTarget?.target_step_id && !validIds.has(failureTarget.target_step_id)) {
      payload.validation_failure_target = null;
    }
    const serialized = JSON.stringify(payload);
    if (signatures.get(flowStep.id) === serialized) continue;
    const updated = await api<AgentStep>(`/agents/${agentId}/steps/${flowStep.id}`, {
      method: "PATCH",
      body: serialized,
    });
    signatures.set(flowStep.id, serialized);
    updatedById.set(flowStep.id, updated);
  }

  // 7. Delete removed steps. Published agents reject a delete while any
  // remaining step still references it, so 422s are deferred to fresh passes.
  let queue = removed;
  while (queue.length > 0) {
    const deferred: Array<{ step: AgentStep; error: ApiError }> = [];
    let progress = false;
    for (const step of queue) {
      try {
        await api<void>(`/agents/${agentId}/steps/${step.id}`, { method: "DELETE" });
        signatures.delete(step.id);
        progress = true;
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 404) {
          signatures.delete(step.id);
          progress = true;
        } else if (reason instanceof ApiError && reason.status === 422) {
          deferred.push({ step, error: reason });
        } else {
          throw reason;
        }
      }
    }
    if (deferred.length === 0) break;
    if (!progress) throw deferred[deferred.length - 1].error;
    queue = deferred.map((item) => item.step);
  }

  // 8. Persist the order only when it changed; otherwise merge the PATCH
  // responses into the known server list.
  const orderedIds: string[] = [];
  const seenIds = new Set<string>();
  for (const flowStep of remapped) {
    if (removedIds.has(flowStep.id) || seenIds.has(flowStep.id)) continue;
    seenIds.add(flowStep.id);
    orderedIds.push(flowStep.id);
  }
  for (const step of ensured.steps) {
    if (step.answer_type !== "outcome" || removedIds.has(step.id) || seenIds.has(step.id)) continue;
    seenIds.add(step.id);
    orderedIds.push(step.id);
  }

  const serverIds = serverSteps.map((step) => step.id);
  const orderChanged =
    orderedIds.length !== serverIds.length ||
    orderedIds.some((id, index) => id !== serverIds[index]);

  let canonical: AgentStep[];
  if (createdById.size > 0 || removed.length > 0 || orderChanged) {
    canonical = await api<AgentStep[]>(`/agents/${agentId}/steps/order`, {
      method: "PUT",
      body: JSON.stringify({ step_ids: orderedIds }),
    });
  } else {
    canonical = serverSteps.map((step) => updatedById.get(step.id) ?? step);
  }

  // 9.
  return { serverSteps: canonical, signatures };
}
