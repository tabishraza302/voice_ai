import type { AgentView, JsonObject, JsonValue } from "@/lib/platform-agent-client";

const RUNTIME_SETTING_KEYS = {
  llm_provider: true,
  llm_model: true,
  stt_provider: true,
  stt_model: true,
  tts_provider: true,
  tts_model: true,
  tts_voice_en: true,
  tts_voice_hi: true,
} as const;

export type FlowStep = JsonObject;
export type FlowDocument = JsonObject;

export interface FlowGraphNode {
  key: string;
  id: string | null;
  title: string;
  type: string | null;
  index: number;
  isEntry: boolean;
  isExit: boolean;
}

export interface FlowGraphEdge {
  fromId: string;
  toId: string | null;
  label: string;
  targetExists: boolean;
}

export interface FlowGraph {
  nodes: FlowGraphNode[];
  edges: FlowGraphEdge[];
}

export interface RuntimeSettingsSummary {
  llmModel: string | null;
  sttProvider: string | null;
  voices: string | null;
}

export interface AgentListQuery {
  query: string;
  enabled: "all" | "enabled" | "disabled";
  publication: "all" | "published" | "unpublished";
  sort: "updated" | "name";
  page: number;
  pageSize: number;
}

export interface AgentListResult {
  items: AgentView[];
  totalItems: number;
  page: number;
  pageCount: number;
}

function isJsonObject(value: JsonValue | undefined): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: JsonValue | undefined): string | null {
  return typeof value === "string" ? value : null;
}

export function getFlowFromConfig(config: JsonObject): FlowDocument {
  if (Object.prototype.hasOwnProperty.call(config, "flow")) {
    const flow = config.flow;
    if (!isJsonObject(flow)) {
      throw new Error("The saved agent configuration has an invalid flow envelope.");
    }
    return flow;
  }

  const flow: JsonObject = {};
  for (const [key, value] of Object.entries(config)) {
    if (!Object.prototype.hasOwnProperty.call(RUNTIME_SETTING_KEYS, key)) {
      flow[key] = value;
    }
  }
  return flow;
}

export function configWithFlow(config: JsonObject, flow: FlowDocument): JsonObject {
  if (Object.prototype.hasOwnProperty.call(config, "flow")) {
    return { ...config, flow };
  }

  const settings: JsonObject = {};
  for (const key of Object.keys(RUNTIME_SETTING_KEYS)) {
    const value = config[key];
    if (value !== undefined) settings[key] = value;
  }
  return { ...flow, ...settings };
}

export function getFlowSteps(flow: FlowDocument): FlowStep[] {
  const steps = flow.steps;
  if (steps === undefined) return [];
  if (!Array.isArray(steps) || !steps.every(isJsonObject)) {
    throw new Error("The saved flow steps must be an array of step objects.");
  }
  return steps as FlowStep[];
}

export function addFlowStep(flow: FlowDocument): FlowDocument {
  const steps = getFlowSteps(flow);
  const usedIds = new Set(steps.map((step) => stringValue(step.id)).filter((id): id is string => Boolean(id)));
  let suffix = 1;
  while (usedIds.has(`step_${suffix}`)) suffix += 1;
  const step: FlowStep = {
    id: `step_${suffix}`,
    title: "New step",
    type: "ask",
    instruction: "",
  };
  return { ...flow, steps: [...steps, step] };
}

export function updateFlowStep(
  flow: FlowDocument,
  index: number,
  updates: Record<string, JsonValue>
): FlowDocument {
  const steps = getFlowSteps(flow);
  if (index < 0 || index >= steps.length) return flow;
  return {
    ...flow,
    steps: steps.map((step, currentIndex) =>
      currentIndex === index ? { ...step, ...updates } : step
    ),
  };
}

export function removeFlowStep(flow: FlowDocument, index: number): FlowDocument {
  const steps = getFlowSteps(flow);
  if (index < 0 || index >= steps.length) return flow;
  return { ...flow, steps: steps.filter((_, currentIndex) => currentIndex !== index) };
}

function conditionLabel(condition: JsonValue | undefined): string {
  if (!isJsonObject(condition)) return "Invalid condition";
  const operator = stringValue(condition.operator);
  const field = stringValue(condition.field);
  if (!operator && Object.keys(condition).length === 0) return "Unconditional";
  if (!operator) return "Condition";
  if (operator === "else") return "else";
  return field ? `${operator} · ${field}` : operator;
}

export function deriveFlowGraph(flow: FlowDocument): FlowGraph {
  const steps = getFlowSteps(flow);
  const ids = steps.map((step) => stringValue(step.id));
  const idSet = new Set(ids.filter((id): id is string => Boolean(id)));
  const entryId = stringValue(flow.entry_step_id);
  const exitId = stringValue(flow.exit_step_id);
  const nodes = steps.map((step, index) => {
    const id = ids[index];
    return {
      key: id ?? `step-${index + 1}`,
      id,
      title: stringValue(step.title) ?? id ?? `Step ${index + 1}`,
      type: stringValue(step.type),
      index,
      isEntry: id !== null && id === entryId,
      isExit: id !== null && id === exitId,
    };
  });
  const edges = steps.flatMap((step, index) => {
    const fromId = ids[index] ?? `step-${index + 1}`;
    const transitions = step.transitions;
    if (!Array.isArray(transitions)) return [];
    return transitions.flatMap((transition) => {
      if (!isJsonObject(transition)) return [];
      const toId = stringValue(transition.to);
      return [{
        fromId,
        toId,
        label: conditionLabel(transition.when),
        targetExists: toId !== null && idSet.has(toId),
      }];
    });
  });
  return { nodes, edges };
}

export function summarizeRuntimeSettings(config: JsonObject): RuntimeSettingsSummary {
  const llmModel = stringValue(config.llm_model);
  const sttProvider = stringValue(config.stt_provider);
  const voiceEn = stringValue(config.tts_voice_en);
  const voiceHi = stringValue(config.tts_voice_hi);
  const voices = voiceEn === null && voiceHi === null
    ? null
    : `${voiceEn ?? "Not set"} / ${voiceHi ?? "Not set"}`;
  return { llmModel, sttProvider, voices };
}

export function getVisibleAgents(agents: AgentView[], query: AgentListQuery): AgentListResult {
  const normalizedQuery = query.query.trim().toLocaleLowerCase("en");
  const filtered = agents.filter((agent) => {
    if (normalizedQuery && !`${agent.name}\n${agent.description}`.toLocaleLowerCase("en").includes(normalizedQuery)) {
      return false;
    }
    if (query.enabled === "enabled" && !agent.enabled) return false;
    if (query.enabled === "disabled" && agent.enabled) return false;
    if (query.publication === "published" && agent.published_version_id === null) return false;
    if (query.publication === "unpublished" && agent.published_version_id !== null) return false;
    return true;
  });

  filtered.sort((left, right) => {
    if (query.sort === "name") {
      return left.name.localeCompare(right.name, "en", { sensitivity: "base" }) || left.id.localeCompare(right.id);
    }
    const updatedOrder = Date.parse(right.updated_at) - Date.parse(left.updated_at);
    return updatedOrder || left.name.localeCompare(right.name, "en", { sensitivity: "base" });
  });

  const pageSize = Math.max(1, Math.floor(query.pageSize));
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const page = Math.min(pageCount, Math.max(1, Math.floor(query.page)));
  const start = (page - 1) * pageSize;
  return {
    items: filtered.slice(start, start + pageSize),
    totalItems: filtered.length,
    page,
    pageCount,
  };
}
