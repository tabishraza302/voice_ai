import { platformApi, type PlatformApiClient } from "@/lib/platform-api-client";

export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export type JsonPrimitive = string | number | boolean | null;
export type JsonObject = { [key: string]: JsonValue };

export interface AgentView {
  id: string;
  name: string;
  description: string;
  persona_prompt: string;
  enabled: boolean;
  draft_revision: number;
  published_version_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentListPage {
  items: AgentView[];
  next_cursor: string | null;
}

export interface AgentCreateRequest {
  name: string;
  description: string;
  source: "manual" | "template" | "generated";
  persona_prompt?: string;
  template_version_id?: string;
}

export interface AgentUpdateRequest {
  name?: string;
  description?: string;
  persona_prompt?: string;
}

export interface AgentDiagnostic {
  code: string;
  path: string;
  message: string;
  severity: "error" | "warning";
}

export interface ValidationSummary {
  errors: AgentDiagnostic[];
  warnings: AgentDiagnostic[];
}

export interface DraftView {
  agent_id: string;
  draft_revision: number;
  authoring_hash: string;
  snapshot_hash: string | null;
  config: JsonObject;
  validation: ValidationSummary;
  updated_at: string;
}

export interface VersionView {
  version_id: string;
  agent_id: string;
  version: number;
  flow_hash: string;
  artifact_hash: string;
  authoring_hash: string;
  compiler_version: string;
  changelog: string;
  created_at: string;
}

export interface VersionListPage {
  items: VersionView[];
  next_cursor: string | null;
}

export interface AgentTemplateView {
  id: string;
  slug: string;
  version: number;
  name: string;
  description: string;
  created_at: string;
}

export interface AgentTemplateListPage {
  items: AgentTemplateView[];
  next_cursor: string | null;
}

export interface DraftValidation {
  valid: boolean;
  eligible_for_publish: boolean;
  authoring_hash: string;
  flow_hash: string | null;
  diagnostics: AgentDiagnostic[];
  errors: AgentDiagnostic[];
  warnings: AgentDiagnostic[];
}

export interface PublishRequest {
  expected_revision: number;
  changelog: string;
  acknowledged_warning_codes: string[];
}

export type GenerationStatus =
  | "QUEUED"
  | "RUNNING"
  | "CANCELLING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED"
  | "EXPIRED";

export interface GenerationModel {
  id: string;
  name: string;
  description: string;
  supports_reasoning: boolean;
}

export interface GenerationJobAccepted {
  job_id: string;
  status: GenerationStatus;
}

export interface AgentProposal {
  name: string;
  description: string;
  persona_prompt: string;
  config: JsonObject;
}

export interface GenerationUsage {
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
}

export interface GenerationJobView {
  status: GenerationStatus;
  proposal: AgentProposal | null;
  unknown_business_facts: string[];
  validation_warnings: AgentDiagnostic[];
  usage: GenerationUsage | null;
}

export interface StartGenerationRequest {
  requirement: string;
  generation_model_id: string;
  language?: "en" | "hi";
  token_budget: number;
}

export type QAScenarioKind =
  | "branch"
  | "refusal"
  | "clarification_exit"
  | "correction"
  | "out_of_order"
  | "multi_answer"
  | "partial_info"
  | "dependency_invalidation"
  | "detour"
  | "contradiction"
  | "missing_business_fact"
  | "early_termination"
  | "prompt_injection";
export type QAJobStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED" | "EXPIRED";

export interface QAExpectedEdge {
  from_step_id: string;
  transition_index: number;
  to_step_id: string;
  when: JsonObject;
}

export interface QAScenarioView {
  scenario_id: string;
  case_key: string;
  kind: QAScenarioKind;
  expected_edge: QAExpectedEdge | null;
  target_step_id: string | null;
  target_fields: string[];
  task: string;
}

export interface QACatalogView {
  scenarios: QAScenarioView[];
  scenario_count: number;
  omitted_scenario_count: number;
  flow_hash: string;
}

export interface QAJobAccepted {
  job_id: string;
  status: "QUEUED";
}

export interface QAUsage {
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
}

export interface QAResultView {
  scenario_id: string;
  kind: QAScenarioKind;
  status: "PASS" | "FAIL" | "INCONCLUSIVE" | "ERROR";
  assertion: string;
  expected_edge: QAExpectedEdge | null;
  observed_edges: JsonObject[];
  turn_count: number;
  token_usage: QAUsage;
  artifact_id: string | null;
  failure_code: string | null;
}

export interface QASuggestionView {
  suggestion_id: string;
  status: "DRAFT" | "APPLIED_TO_DRAFT" | "DISMISSED";
  draft_only: boolean;
  title: string;
  summary: string;
  target_step_id: string | null;
  target_fields: string[];
  evidence: JsonObject;
  proposed_patch: { path: string; append_text: string } | null;
}

export interface QAJobView {
  job_id: string;
  agent_id: string;
  snapshot_kind: "draft" | "published";
  snapshot_hash: string;
  status: QAJobStatus;
  config_hash: string;
  rubric_version: string;
  models: { runtime: string | null; utterance: string | null; judge: string | null };
  scenarios: QAScenarioView[];
  results: QAResultView[];
  suggestions: QASuggestionView[];
  coverage: JsonObject;
  budget: {
    scenario_count: number;
    max_turns: number;
    max_tokens: number;
    max_runtime_seconds: number;
  };
  usage: QAUsage | null;
  failure_code: string | null;
  runtime_seconds: number | null;
  artifacts: JsonObject[];
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface StartQAJobRequest {
  agent_id: string;
  snapshot_hash: string;
  scenario_count: number;
  scenario_ids: string[];
  max_turns: number;
  max_tokens: number;
  max_runtime_seconds: number;
  utterance_model_id: string;
  judge_model_id?: string | null;
}

export interface QASuggestionApplied {
  suggestion_id: string;
  draft_revision: number;
  authoring_hash: string;
  applied: true;
}

export interface QASuggestionDismissed {
  suggestion_id: string;
  status: "DISMISSED";
}

export interface QACancelResult {
  job_id: string;
  status: QAJobStatus;
}

export type ChatOutcome =
  | "in_progress"
  | "completed"
  | "partial"
  | "aborted"
  | "failed";

export interface ChatTraceDiagnostic {
  code: string;
  path: string;
  severity: "error" | "warning";
  message?: string;
}

export interface ChatTraceEntry {
  turn_sequence: number;
  event_type: string;
  step_id: string | null;
  state_version: number;
  outcome: string;
  diagnostics: ChatTraceDiagnostic[];
}

export interface ChatMessage {
  id: string;
  turn_sequence: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export interface ChatTurnRequest {
  snapshot_hash: string;
  test_id?: string;
  expected_turn_sequence: number;
  text: string;
}

export interface ChatTurnResponse {
  test_id: string;
  turn_sequence: number;
  reply: string;
  flow_state: JsonObject;
  trace: ChatTraceEntry[];
  outcome: ChatOutcome;
}

export interface ChatTestView {
  test_id: string;
  snapshot_hash: string;
  turn_sequence: number;
  pending_turn_sequence: number | null;
  messages: ChatMessage[];
  flow_state: JsonObject;
  trace: ChatTraceEntry[];
  outcome: ChatOutcome;
}

export interface ListOptions {
  cursor?: string;
  limit?: number;
}

const GENERATION_STATUSES = new Set<GenerationStatus>([
  "QUEUED",
  "RUNNING",
  "CANCELLING",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
  "EXPIRED",
]);
const CHAT_OUTCOMES = new Set<ChatOutcome>([
  "in_progress",
  "completed",
  "partial",
  "aborted",
  "failed",
]);
const QA_JOB_STATUSES: Record<QAJobStatus, true> = {
  QUEUED: true,
  RUNNING: true,
  COMPLETED: true,
  FAILED: true,
  CANCELLED: true,
  EXPIRED: true,
};
const QA_SCENARIO_KINDS: Record<QAScenarioKind, true> = {
  branch: true,
  refusal: true,
  clarification_exit: true,
  correction: true,
  out_of_order: true,
  multi_answer: true,
  partial_info: true,
  dependency_invalidation: true,
  detour: true,
  contradiction: true,
  missing_business_fact: true,
  early_termination: true,
  prompt_injection: true,
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireRecord(value: unknown, name: string): Record<string, unknown> {
  if (!isRecord(value)) throw new Error(`Invalid platform response: ${name} must be an object.`);
  return value;
}

function requireString(value: unknown, name: string): string {
  if (typeof value !== "string") throw new Error(`Invalid platform response: ${name} must be a string.`);
  return value;
}

function requireNullableString(value: unknown, name: string): string | null {
  if (value === null) return null;
  return requireString(value, name);
}

function requireNumber(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`Invalid platform response: ${name} must be a number.`);
  }
  return value;
}
function requireNullableNumber(value: unknown, name: string): number | null {
  if (value === null) return null;
  return requireNumber(value, name);
}

function requireBoolean(value: unknown, name: string): boolean {
  if (typeof value !== "boolean") throw new Error(`Invalid platform response: ${name} must be a boolean.`);
  return value;
}

function isJsonValue(value: unknown): value is JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJsonValue);
  return isRecord(value) && Object.values(value).every(isJsonValue);
}

function requireJsonObject(value: unknown, name: string): JsonObject {
  if (!isRecord(value) || !Object.values(value).every(isJsonValue)) {
    throw new Error(`Invalid platform response: ${name} must be a JSON object.`);
  }
  return value as JsonObject;
}

function requireStringArray(value: unknown, name: string): string[] {
  if (!Array.isArray(value)) throw new Error(`Invalid platform response: ${name} must be an array.`);
  return value.map((item, index) => requireString(item, `${name}[${index}]`));
}

function parseAgent(value: unknown): AgentView {
  const row = requireRecord(value, "agent");
  const publishedVersion = row.published_version_id;
  if (publishedVersion !== null && typeof publishedVersion !== "string") {
    throw new Error("Invalid platform response: agent.published_version_id must be a string or null.");
  }
  return {
    id: requireString(row.id, "agent.id"),
    name: requireString(row.name, "agent.name"),
    description: requireString(row.description, "agent.description"),
    persona_prompt: requireString(row.persona_prompt, "agent.persona_prompt"),
    enabled: requireBoolean(row.enabled, "agent.enabled"),
    draft_revision: requireNumber(row.draft_revision, "agent.draft_revision"),
    published_version_id: publishedVersion,
    created_at: requireString(row.created_at, "agent.created_at"),
    updated_at: requireString(row.updated_at, "agent.updated_at"),
  };
}

function parseDiagnostic(value: unknown): AgentDiagnostic {
  const row = requireRecord(value, "diagnostic");
  const severity = row.severity;
  if (severity !== "error" && severity !== "warning") {
    throw new Error("Invalid platform response: diagnostic.severity is not supported.");
  }
  return {
    code: requireString(row.code, "diagnostic.code"),
    path: requireString(row.path, "diagnostic.path"),
    message: requireString(row.message, "diagnostic.message"),
    severity,
  };
}

function parseDiagnostics(value: unknown, name: string): AgentDiagnostic[] {
  if (!Array.isArray(value)) throw new Error(`Invalid platform response: ${name} must be an array.`);
  return value.map(parseDiagnostic);
}

function parseValidationSummary(value: unknown): ValidationSummary {
  const row = requireRecord(value, "draft.validation");
  return {
    errors: parseDiagnostics(row.errors, "draft.validation.errors"),
    warnings: parseDiagnostics(row.warnings, "draft.validation.warnings"),
  };
}

function parseDraft(value: unknown): DraftView {
  const row = requireRecord(value, "draft");
  return {
    agent_id: requireString(row.agent_id, "draft.agent_id"),
    draft_revision: requireNumber(row.draft_revision, "draft.draft_revision"),
    authoring_hash: requireString(row.authoring_hash, "draft.authoring_hash"),
    snapshot_hash: requireNullableString(row.snapshot_hash, "draft.snapshot_hash"),
    config: requireJsonObject(row.config, "draft.config"),
    validation: parseValidationSummary(row.validation),
    updated_at: requireString(row.updated_at, "draft.updated_at"),
  };
}

function parseVersion(value: unknown): VersionView {
  const row = requireRecord(value, "version");
  return {
    version_id: requireString(row.version_id, "version.version_id"),
    agent_id: requireString(row.agent_id, "version.agent_id"),
    version: requireNumber(row.version, "version.version"),
    flow_hash: requireString(row.flow_hash, "version.flow_hash"),
    artifact_hash: requireString(row.artifact_hash, "version.artifact_hash"),
    authoring_hash: requireString(row.authoring_hash, "version.authoring_hash"),
    compiler_version: requireString(row.compiler_version, "version.compiler_version"),
    changelog: requireString(row.changelog, "version.changelog"),
    created_at: requireString(row.created_at, "version.created_at"),
  };
}

function parseCursorPage<T>(
  value: unknown,
  name: string,
  parseItem: (item: unknown) => T
): { items: T[]; next_cursor: string | null } {
  const row = requireRecord(value, name);
  if (!Array.isArray(row.items)) throw new Error(`Invalid platform response: ${name}.items must be an array.`);
  return {
    items: row.items.map(parseItem),
    next_cursor: requireNullableString(row.next_cursor, `${name}.next_cursor`),
  };
}

function parseTemplate(value: unknown): AgentTemplateView {
  const row = requireRecord(value, "template");
  return {
    id: requireString(row.id, "template.id"),
    slug: requireString(row.slug, "template.slug"),
    version: requireNumber(row.version, "template.version"),
    name: requireString(row.name, "template.name"),
    description: requireString(row.description, "template.description"),
    created_at: requireString(row.created_at, "template.created_at"),
  };
}

function parseDraftValidation(value: unknown): DraftValidation {
  const row = requireRecord(value, "draft validation");
  const flowHash = row.flow_hash;
  if (flowHash !== null && typeof flowHash !== "string") {
    throw new Error("Invalid platform response: validation.flow_hash must be a string or null.");
  }
  return {
    valid: requireBoolean(row.valid, "validation.valid"),
    eligible_for_publish: requireBoolean(row.eligible_for_publish, "validation.eligible_for_publish"),
    authoring_hash: requireString(row.authoring_hash, "validation.authoring_hash"),
    flow_hash: flowHash,
    diagnostics: parseDiagnostics(row.diagnostics, "validation.diagnostics"),
    errors: parseDiagnostics(row.errors, "validation.errors"),
    warnings: parseDiagnostics(row.warnings, "validation.warnings"),
  };
}

function parseGenerationStatus(value: unknown): GenerationStatus {
  if (typeof value !== "string" || !GENERATION_STATUSES.has(value as GenerationStatus)) {
    throw new Error("Invalid platform response: generation status is not supported.");
  }
  return value as GenerationStatus;
}

function parseGenerationModel(value: unknown): GenerationModel {
  const row = requireRecord(value, "generation model");
  return {
    id: requireString(row.id, "generation_model.id"),
    name: requireString(row.name, "generation_model.name"),
    description: requireString(row.description, "generation_model.description"),
    supports_reasoning: requireBoolean(row.supports_reasoning, "generation_model.supports_reasoning"),
  };
}

function parseProposal(value: unknown): AgentProposal {
  const row = requireRecord(value, "generation proposal");
  return {
    name: requireString(row.name, "proposal.name"),
    description: requireString(row.description, "proposal.description"),
    persona_prompt: requireString(row.persona_prompt, "proposal.persona_prompt"),
    config: requireJsonObject(row.config, "proposal.config"),
  };
}

function parseGenerationUsage(value: unknown): GenerationUsage | null {
  if (value === null) return null;
  const row = requireRecord(value, "generation usage");
  return {
    prompt_tokens: requireNullableNumber(row.prompt_tokens, "usage.prompt_tokens"),
    completion_tokens: requireNullableNumber(row.completion_tokens, "usage.completion_tokens"),
    total_tokens: requireNullableNumber(row.total_tokens, "usage.total_tokens"),
  };
}

function parseGenerationAccepted(value: unknown): GenerationJobAccepted {
  const row = requireRecord(value, "generation job accepted");
  return {
    job_id: requireString(row.job_id, "generation_job.job_id"),
    status: parseGenerationStatus(row.status),
  };
}

function parseGenerationJob(value: unknown): GenerationJobView {
  const row = requireRecord(value, "generation job");
  const proposal = row.proposal;
  return {
    status: parseGenerationStatus(row.status),
    proposal: proposal === null ? null : parseProposal(proposal),
    unknown_business_facts: requireStringArray(row.unknown_business_facts, "unknown_business_facts"),
    validation_warnings: parseDiagnostics(row.validation_warnings, "validation_warnings"),
    usage: parseGenerationUsage(row.usage),
  };
}

function parseQAStatus(value: unknown): QAJobStatus {
  if (typeof value !== "string" || QA_JOB_STATUSES[value as QAJobStatus] !== true) {
    throw new Error("Invalid platform response: QA job status is not supported.");
  }
  return value as QAJobStatus;
}

function parseQAKind(value: unknown): QAScenarioKind {
  if (typeof value !== "string" || QA_SCENARIO_KINDS[value as QAScenarioKind] !== true) {
    throw new Error("Invalid platform response: QA scenario kind is not supported.");
  }
  return value as QAScenarioKind;
}

function parseQAExpectedEdge(value: unknown): QAExpectedEdge | null {
  if (value === null) return null;
  const row = requireRecord(value, "QA expected edge");
  return {
    from_step_id: requireString(row.from_step_id, "QA expected edge.from_step_id"),
    transition_index: requireNumber(row.transition_index, "QA expected edge.transition_index"),
    to_step_id: requireString(row.to_step_id, "QA expected edge.to_step_id"),
    when: requireJsonObject(row.when, "QA expected edge.when"),
  };
}

function parseQAScenario(value: unknown): QAScenarioView {
  const row = requireRecord(value, "QA scenario");
  return {
    scenario_id: requireString(row.scenario_id, "QA scenario.scenario_id"),
    case_key: requireString(row.case_key, "QA scenario.case_key"),
    kind: parseQAKind(row.kind),
    expected_edge: parseQAExpectedEdge(row.expected_edge),
    target_step_id: requireNullableString(row.target_step_id, "QA scenario.target_step_id"),
    target_fields: requireStringArray(row.target_fields, "QA scenario.target_fields"),
    task: requireString(row.task, "QA scenario.task"),
  };
}

function parseQAUsage(value: unknown, name: string): QAUsage | null {
  if (value === null) return null;
  const row = requireRecord(value, name);
  return {
    prompt_tokens: requireNullableNumber(row.prompt_tokens, `${name}.prompt_tokens`),
    completion_tokens: requireNullableNumber(row.completion_tokens, `${name}.completion_tokens`),
    total_tokens: requireNullableNumber(row.total_tokens, `${name}.total_tokens`),
  };
}

function parseQACatalog(value: unknown): QACatalogView {
  const row = requireRecord(value, "QA scenario catalog");
  if (!Array.isArray(row.scenarios)) {
    throw new Error("Invalid platform response: QA scenario catalog.scenarios must be an array.");
  }
  return {
    scenarios: row.scenarios.map(parseQAScenario),
    scenario_count: requireNumber(row.scenario_count, "QA scenario catalog.scenario_count"),
    omitted_scenario_count: requireNumber(
      row.omitted_scenario_count,
      "QA scenario catalog.omitted_scenario_count",
    ),
    flow_hash: requireString(row.flow_hash, "QA scenario catalog.flow_hash"),
  };
}

function parseQAResult(value: unknown): QAResultView {
  const row = requireRecord(value, "QA result");
  const status = row.status;
  if (status !== "PASS" && status !== "FAIL" && status !== "INCONCLUSIVE" && status !== "ERROR") {
    throw new Error("Invalid platform response: QA result status is not supported.");
  }
  if (!Array.isArray(row.observed_edges)) {
    throw new Error("Invalid platform response: QA result.observed_edges must be an array.");
  }
  const usage = parseQAUsage(row.token_usage, "QA result.token_usage");
  if (usage === null) throw new Error("Invalid platform response: QA result.token_usage must be an object.");
  return {
    scenario_id: requireString(row.scenario_id, "QA result.scenario_id"),
    kind: parseQAKind(row.kind),
    status,
    assertion: requireString(row.assertion, "QA result.assertion"),
    expected_edge: parseQAExpectedEdge(row.expected_edge),
    observed_edges: row.observed_edges.map((edge, index) => requireJsonObject(edge, `QA result.observed_edges[${index}]`)),
    turn_count: requireNumber(row.turn_count, "QA result.turn_count"),
    token_usage: usage,
    artifact_id: requireNullableString(row.artifact_id, "QA result.artifact_id"),
    failure_code: requireNullableString(row.failure_code, "QA result.failure_code"),
  };
}

function parseQASuggestion(value: unknown): QASuggestionView {
  const row = requireRecord(value, "QA suggestion");
  const status = row.status;
  if (status !== "DRAFT" && status !== "APPLIED_TO_DRAFT" && status !== "DISMISSED") {
    throw new Error("Invalid platform response: QA suggestion status is not supported.");
  }
  const patch = row.proposed_patch;
  let proposedPatch: QASuggestionView["proposed_patch"] = null;
  if (patch !== null) {
    const patchRow = requireRecord(patch, "QA suggestion.proposed_patch");
    proposedPatch = {
      path: requireString(patchRow.path, "QA suggestion.proposed_patch.path"),
      append_text: requireString(patchRow.append_text, "QA suggestion.proposed_patch.append_text"),
    };
  }
  return {
    suggestion_id: requireString(row.suggestion_id, "QA suggestion.suggestion_id"),
    status,
    draft_only: requireBoolean(row.draft_only, "QA suggestion.draft_only"),
    title: requireString(row.title, "QA suggestion.title"),
    summary: requireString(row.summary, "QA suggestion.summary"),
    target_step_id: requireNullableString(row.target_step_id, "QA suggestion.target_step_id"),
    target_fields: requireStringArray(row.target_fields, "QA suggestion.target_fields"),
    evidence: requireJsonObject(row.evidence, "QA suggestion.evidence"),
    proposed_patch: proposedPatch,
  };
}

function parseQAJobAccepted(value: unknown): QAJobAccepted {
  const row = requireRecord(value, "QA job accepted");
  if (row.status !== "QUEUED") throw new Error("Invalid platform response: QA job was not queued.");
  return { job_id: requireString(row.job_id, "QA job.job_id"), status: "QUEUED" };
}

function parseQACancelResult(value: unknown): QACancelResult {
  const row = requireRecord(value, "QA cancellation");
  return {
    job_id: requireString(row.job_id, "QA cancellation.job_id"),
    status: parseQAStatus(row.status),
  };
}

function parseQAJob(value: unknown): QAJobView {
  const row = requireRecord(value, "QA job");
  const models = requireRecord(row.models, "QA job.models");
  const budget = requireRecord(row.budget, "QA job.budget");
  const usage = parseQAUsage(row.usage, "QA job.usage");
  const snapshotKind = row.snapshot_kind;
  if (snapshotKind !== "draft" && snapshotKind !== "published") {
    throw new Error("Invalid platform response: QA job.snapshot_kind is not supported.");
  }
  if (!Array.isArray(row.scenarios) || !Array.isArray(row.results) ||
      !Array.isArray(row.suggestions) || !Array.isArray(row.artifacts)) {
    throw new Error("Invalid platform response: QA job collections must be arrays.");
  }
  return {
    job_id: requireString(row.job_id, "QA job.job_id"),
    agent_id: requireString(row.agent_id, "QA job.agent_id"),
    snapshot_kind: snapshotKind,
    snapshot_hash: requireString(row.snapshot_hash, "QA job.snapshot_hash"),
    status: parseQAStatus(row.status),
    config_hash: requireString(row.config_hash, "QA job.config_hash"),
    rubric_version: requireString(row.rubric_version, "QA job.rubric_version"),
    models: {
      runtime: requireNullableString(models.runtime, "QA job.models.runtime"),
      utterance: requireNullableString(models.utterance, "QA job.models.utterance"),
      judge: requireNullableString(models.judge, "QA job.models.judge"),
    },
    scenarios: row.scenarios.map(parseQAScenario),
    results: row.results.map(parseQAResult),
    suggestions: row.suggestions.map(parseQASuggestion),
    coverage: requireJsonObject(row.coverage, "QA job.coverage"),
    budget: {
      scenario_count: requireNumber(budget.scenario_count, "QA job.budget.scenario_count"),
      max_turns: requireNumber(budget.max_turns, "QA job.budget.max_turns"),
      max_tokens: requireNumber(budget.max_tokens, "QA job.budget.max_tokens"),
      max_runtime_seconds: requireNumber(budget.max_runtime_seconds, "QA job.budget.max_runtime_seconds"),
    },
    usage,
    failure_code: requireNullableString(row.failure_code, "QA job.failure_code"),
    runtime_seconds: requireNullableNumber(row.runtime_seconds, "QA job.runtime_seconds"),
    artifacts: row.artifacts.map((artifact, index) => requireJsonObject(artifact, `QA job.artifacts[${index}]`)),
    created_at: requireString(row.created_at, "QA job.created_at"),
    started_at: requireNullableString(row.started_at, "QA job.started_at"),
    completed_at: requireNullableString(row.completed_at, "QA job.completed_at"),
  };
}

function parseQASuggestionApplied(value: unknown): QASuggestionApplied {
  const row = requireRecord(value, "applied QA suggestion");
  if (row.applied !== true) throw new Error("Invalid platform response: QA suggestion was not applied.");
  return {
    suggestion_id: requireString(row.suggestion_id, "QA suggestion.suggestion_id"),
    draft_revision: requireNumber(row.draft_revision, "QA suggestion.draft_revision"),
    authoring_hash: requireString(row.authoring_hash, "QA suggestion.authoring_hash"),
    applied: true,
  };
}

function parseQASuggestionDismissed(value: unknown): QASuggestionDismissed {
  const row = requireRecord(value, "dismissed QA suggestion");
  if (row.status !== "DISMISSED") throw new Error("Invalid platform response: QA suggestion was not dismissed.");
  return {
    suggestion_id: requireString(row.suggestion_id, "QA suggestion.suggestion_id"),
    status: "DISMISSED",
  };
}

function parseChatOutcome(value: unknown): ChatOutcome {
  if (typeof value !== "string" || !CHAT_OUTCOMES.has(value as ChatOutcome)) {
    throw new Error("Invalid platform response: chat outcome is not supported.");
  }
  return value as ChatOutcome;
}

function parseTraceDiagnostic(value: unknown): ChatTraceDiagnostic {
  const row = requireRecord(value, "chat trace diagnostic");
  const severity = row.severity;
  if (severity !== "error" && severity !== "warning") {
    throw new Error("Invalid platform response: trace diagnostic severity is not supported.");
  }
  return {
    code: requireString(row.code, "trace diagnostic.code"),
    path: requireString(row.path, "trace diagnostic.path"),
    ...(row.message === undefined ? {} : { message: requireString(row.message, "trace diagnostic.message") }),
    severity,
  };
}

function parseTrace(value: unknown): ChatTraceEntry[] {
  if (!Array.isArray(value)) throw new Error("Invalid platform response: chat trace must be an array.");
  return value.map((entry) => {
    const row = requireRecord(entry, "chat trace entry");
    if (!Array.isArray(row.diagnostics)) {
      throw new Error("Invalid platform response: trace diagnostics must be an array.");
    }
    return {
      turn_sequence: requireNumber(row.turn_sequence, "trace.turn_sequence"),
      event_type: requireString(row.event_type, "trace.event_type"),
      step_id: requireNullableString(row.step_id, "trace.step_id"),
      state_version: requireNumber(row.state_version, "trace.state_version"),
      outcome: requireString(row.outcome, "trace.outcome"),
      diagnostics: row.diagnostics.map(parseTraceDiagnostic),
    };
  });
}

function parseChatTurn(value: unknown): ChatTurnResponse {
  const row = requireRecord(value, "chat turn");
  return {
    test_id: requireString(row.test_id, "chat.test_id"),
    turn_sequence: requireNumber(row.turn_sequence, "chat.turn_sequence"),
    reply: requireString(row.reply, "chat.reply"),
    flow_state: requireJsonObject(row.flow_state, "chat.flow_state"),
    trace: parseTrace(row.trace),
    outcome: parseChatOutcome(row.outcome),
  };
}

function parseChatMessage(value: unknown): ChatMessage {
  const row = requireRecord(value, "chat message");
  if (row.role !== "user" && row.role !== "assistant") {
    throw new Error("Invalid platform response: chat message role is not supported.");
  }
  return {
    id: requireString(row.id, "message.id"),
    turn_sequence: requireNumber(row.turn_sequence, "message.turn_sequence"),
    role: row.role,
    content: requireString(row.content, "message.content"),
    created_at: requireString(row.created_at, "message.created_at"),
  };
}

function parseChatTest(value: unknown): ChatTestView {
  const row = requireRecord(value, "chat test");
  if (!Array.isArray(row.messages)) {
    throw new Error("Invalid platform response: chat test messages must be an array.");
  }
  return {
    test_id: requireString(row.test_id, "chat_test.test_id"),
    snapshot_hash: requireString(row.snapshot_hash, "chat_test.snapshot_hash"),
    turn_sequence: requireNumber(row.turn_sequence, "chat_test.turn_sequence"),
    pending_turn_sequence: requireNullableNumber(row.pending_turn_sequence, "chat_test.pending_turn_sequence"),
    messages: row.messages.map(parseChatMessage),
    flow_state: requireJsonObject(row.flow_state, "chat_test.flow_state"),
    trace: parseTrace(row.trace),
    outcome: parseChatOutcome(row.outcome),
  };
}

function segment(value: string): string {
  return encodeURIComponent(value);
}

function queryPath(path: string, options: ListOptions = {}): string {
  const query = new URLSearchParams();
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.cursor) query.set("cursor", options.cursor);
  const serialized = query.toString();
  return serialized ? `${path}?${serialized}` : path;
}

function idempotencyKey(): string {
  if (!globalThis.crypto?.randomUUID) {
    throw new Error("This browser cannot create an idempotency key for the requested action.");
  }
  return globalThis.crypto.randomUUID();
}

export class PlatformAgentClient {
  constructor(private readonly api: Pick<PlatformApiClient, "request" | "getCsrfToken" | "setCsrfToken"> = platformApi) {}

  setCsrfToken(token: string | null): void {
    this.api.setCsrfToken(token);
  }

  async listAgents(options: ListOptions = {}): Promise<AgentListPage> {
    return parseCursorPage(
      await this.api.request<unknown>(queryPath("/agents", { limit: options.limit ?? 100, cursor: options.cursor })),
      "agent list",
      parseAgent
    );
  }

  async listAllAgents(): Promise<AgentView[]> {
    const agents: AgentView[] = [];
    const seenCursors = new Set<string>();
    let cursor: string | undefined;
    do {
      const page = await this.listAgents({ limit: 100, cursor });
      agents.push(...page.items);
      cursor = page.next_cursor ?? undefined;
      if (cursor && seenCursors.has(cursor)) {
        throw new Error("The agent list returned a repeated cursor; reload the page to try again.");
      }
      if (cursor) seenCursors.add(cursor);
    } while (cursor);
    return agents;
  }

  async getAgent(agentId: string): Promise<AgentView> {
    return parseAgent(await this.api.request<unknown>(`/agents/${segment(agentId)}`));
  }

  async createAgent(
    request: AgentCreateRequest,
    key: string = idempotencyKey()
  ): Promise<AgentView> {
    return parseAgent(await this.api.request<unknown>("/agents", {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify(request),
    }));
  }

  async updateAgent(agentId: string, request: AgentUpdateRequest): Promise<AgentView> {
    return parseAgent(await this.api.request<unknown>(`/agents/${segment(agentId)}`, {
      method: "PATCH",
      body: JSON.stringify(request),
    }));
  }

  async deleteAgent(agentId: string): Promise<void> {
    await this.api.request<void>(`/agents/${segment(agentId)}`, { method: "DELETE" });
  }

  async duplicateAgent(
    agentId: string,
    request: { name: string; description?: string; persona_prompt?: string; source_revision_or_version?: number | string },
    key: string = idempotencyKey()
  ): Promise<AgentView> {
    return parseAgent(await this.api.request<unknown>(`/agents/${segment(agentId)}/duplicate`, {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify(request),
    }));
  }

  async setEnabled(agentId: string, enabled: boolean): Promise<AgentView> {
    const action = enabled ? "enable" : "disable";
    return parseAgent(await this.api.request<unknown>(`/agents/${segment(agentId)}/${action}`, {
      method: "POST",
      body: JSON.stringify({}),
    }));
  }

  async getDraft(agentId: string): Promise<DraftView> {
    return parseDraft(await this.api.request<unknown>(`/agents/${segment(agentId)}/draft`));
  }

  async patchDraft(
    agentId: string,
    request: { expected_revision: number; config: JsonObject }
  ): Promise<DraftView> {
    return parseDraft(await this.api.request<unknown>(`/agents/${segment(agentId)}/draft`, {
      method: "PATCH",
      body: JSON.stringify(request),
    }));
  }

  async validateDraft(
    agentId: string,
    request: { expected_revision?: number; expected_authoring_hash?: string; config?: JsonObject } = {}
  ): Promise<DraftValidation> {
    return parseDraftValidation(await this.api.request<unknown>(`/agents/${segment(agentId)}/validate`, {
      method: "POST",
      body: JSON.stringify(request),
    }));
  }

  async publishDraft(
    agentId: string,
    request: PublishRequest,
    key: string = idempotencyKey()
  ): Promise<VersionView> {
    return parseVersion(await this.api.request<unknown>(`/agents/${segment(agentId)}/publish`, {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify(request),
    }));
  }

  async listVersions(agentId: string, options: ListOptions = {}): Promise<VersionListPage> {
    return parseCursorPage(
      await this.api.request<unknown>(queryPath(`/agents/${segment(agentId)}/versions`, { limit: options.limit ?? 100, cursor: options.cursor })),
      "version list",
      parseVersion
    );
  }

  async listAllVersions(agentId: string): Promise<VersionView[]> {
    const versions: VersionView[] = [];
    const seenCursors = new Set<string>();
    let cursor: string | undefined;
    do {
      const page = await this.listVersions(agentId, { limit: 100, cursor });
      versions.push(...page.items);
      cursor = page.next_cursor ?? undefined;
      if (cursor && seenCursors.has(cursor)) {
        throw new Error("The version history returned a repeated cursor; reload the page to try again.");
      }
      if (cursor) seenCursors.add(cursor);
    } while (cursor);
    return versions;
  }

  async restoreDraft(
    agentId: string,
    request: { version_id: string; expected_revision: number }
  ): Promise<DraftView> {
    return parseDraft(await this.api.request<unknown>(`/agents/${segment(agentId)}/restore-draft`, {
      method: "POST",
      body: JSON.stringify(request),
    }));
  }

  async listTemplates(options: ListOptions = {}): Promise<AgentTemplateListPage> {
    return parseCursorPage(
      await this.api.request<unknown>(queryPath("/templates", { limit: options.limit ?? 100, cursor: options.cursor })),
      "template list",
      parseTemplate
    );
  }

  async listAllTemplates(): Promise<AgentTemplateView[]> {
    const templates: AgentTemplateView[] = [];
    const seenCursors = new Set<string>();
    let cursor: string | undefined;
    do {
      const page = await this.listTemplates({ limit: 100, cursor });
      templates.push(...page.items);
      cursor = page.next_cursor ?? undefined;
      if (cursor && seenCursors.has(cursor)) {
        throw new Error("The template catalog returned a repeated cursor; reload the page to try again.");
      }
      if (cursor) seenCursors.add(cursor);
    } while (cursor);
    return templates;
  }

  async listGenerationModels(): Promise<GenerationModel[]> {
    const value = requireRecord(await this.api.request<unknown>("/generation-models"), "generation model list");
    if (!Array.isArray(value.items)) {
      throw new Error("Invalid platform response: generation model list.items must be an array.");
    }
    return value.items.map(parseGenerationModel);
  }

  async listQAScenarios(
    agentId: string,
    snapshotHash: string,
    scenarioCount: number,
  ): Promise<QACatalogView> {
    const query = new URLSearchParams({
      agent_id: agentId,
      snapshot_hash: snapshotHash,
      scenario_count: String(scenarioCount),
    });
    return parseQACatalog(await this.api.request<unknown>(`/qa/scenarios?${query.toString()}`));
  }

  async createQAJob(request: StartQAJobRequest): Promise<QAJobAccepted> {
    return parseQAJobAccepted(await this.api.request<unknown>("/qa/jobs", {
      method: "POST",
      body: JSON.stringify(request),
    }));
  }

  async getQAJob(jobId: string): Promise<QAJobView> {
    return parseQAJob(await this.api.request<unknown>(`/qa/jobs/${segment(jobId)}`));
  }

  async cancelQAJob(jobId: string): Promise<QACancelResult> {
    return parseQACancelResult(await this.api.request<unknown>(`/qa/jobs/${segment(jobId)}/cancel`, {
      method: "POST",
      body: JSON.stringify({}),
    }));
  }

  async applyQASuggestion(
    jobId: string,
    suggestionId: string,
    expectedRevision: number,
  ): Promise<QASuggestionApplied> {
    return parseQASuggestionApplied(await this.api.request<unknown>(
      `/qa/jobs/${segment(jobId)}/suggestions/${segment(suggestionId)}/apply`,
      { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) },
    ));
  }

  async dismissQASuggestion(
    jobId: string,
    suggestionId: string,
  ): Promise<QASuggestionDismissed> {
    return parseQASuggestionDismissed(await this.api.request<unknown>(
      `/qa/jobs/${segment(jobId)}/suggestions/${segment(suggestionId)}/dismiss`,
      { method: "POST", body: JSON.stringify({}) },
    ));
  }

  async startGenerationJob(request: StartGenerationRequest): Promise<GenerationJobAccepted> {
    return parseGenerationAccepted(await this.api.request<unknown>("/agents/generate", {
      method: "POST",
      body: JSON.stringify(request),
    }));
  }

  async getGenerationJob(jobId: string): Promise<GenerationJobView> {
    return parseGenerationJob(await this.api.request<unknown>(`/generation-jobs/${segment(jobId)}`));
  }
  async cancelGenerationJob(jobId: string): Promise<GenerationJobAccepted> {
    return parseGenerationAccepted(await this.api.request<unknown>(`/generation-jobs/${segment(jobId)}/cancel`, {
      method: "POST",
      body: JSON.stringify({}),
    }));
  }


  async sendChatTurn(agentId: string, request: ChatTurnRequest): Promise<ChatTurnResponse> {
    return parseChatTurn(await this.api.request<unknown>(`/agents/${segment(agentId)}/test/chat`, {
      method: "POST",
      body: JSON.stringify(request),
    }));
  }

  async getChatTest(agentId: string, testId: string): Promise<ChatTestView> {
    return parseChatTest(await this.api.request<unknown>(`/agents/${segment(agentId)}/test/chat/${segment(testId)}`));
  }
}

export const platformAgentApi = new PlatformAgentClient();
