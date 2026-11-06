import { ApiError, api } from "@/lib/api";
import type { AgentStep, StepInput } from "@/lib/types";

import { callFlowToStepInput } from "./call-flow-adapter";
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
 * creates new steps, patches changed steps, deletes removed steps, then
 * persists the order only when it actually changed.
 */
export async function saveCallFlow(input: CallFlowSaveInput): Promise<CallFlowSaveOutput> {
  const { agentId, serverSteps, signatures, onIdsMapped } = input;
  let remapped = input.model;

  // 1. Server steps that vanished from the model
  const modelIds = new Set(remapped.map((step) => step.id));
  const removed = serverSteps.filter((step) => !modelIds.has(step.id));
  const removedIds = new Set(removed.map((step) => step.id));

  // 2. Create new steps (no rawStep) in model order. Remap the local snapshot
  // after each successful POST so a later failure can't re-create anything.
  const idMap = new Map<string, string>();
  const createdById = new Map<string, AgentStep>();
  for (let index = 0; index < remapped.length; index++) {
    const flowStep = remapped[index];
    if (flowStep.rawStep) continue;
    const created = await api<AgentStep>(`/agents/${agentId}/steps`, {
      method: "POST",
      body: JSON.stringify({
        ...callFlowToStepInput(flowStep, remapped, index),
        branches: [],
      }),
    });
    idMap.set(flowStep.id, created.id);
    createdById.set(flowStep.id, created);
    remapped = remapFlowSteps(remapped, idMap, createdById);
    onIdsMapped(idMap, createdById);
  }

  // 3. PATCH steps whose serialized payload changed
  const updatedById = new Map<string, AgentStep>();
  for (let index = 0; index < remapped.length; index++) {
    const flowStep = remapped[index];
    const payload: StepInput = callFlowToStepInput(flowStep, remapped, index);
    const serialized = JSON.stringify(payload);
    if (signatures.get(flowStep.id) === serialized) continue;
    const updated = await api<AgentStep>(`/agents/${agentId}/steps/${flowStep.id}`, {
      method: "PATCH",
      body: serialized,
    });
    signatures.set(flowStep.id, serialized);
    updatedById.set(flowStep.id, updated);
  }

  // 4. Delete removed steps. Published agents reject a delete while any
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

  // 5. Persist the order only when it changed; otherwise merge the PATCH
  // responses into the known server list.
  const orderedIds: string[] = [];
  const seenIds = new Set<string>();
  for (const flowStep of remapped) {
    if (removedIds.has(flowStep.id) || seenIds.has(flowStep.id)) continue;
    seenIds.add(flowStep.id);
    orderedIds.push(flowStep.id);
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

  return { serverSteps: canonical, signatures };
}
