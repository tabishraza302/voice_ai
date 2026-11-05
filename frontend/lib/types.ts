export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export type UserRole = "user" | "admin";

export interface CurrentUser {
  id: string;
  email: string;
  role: UserRole;
  csrf_token?: string;
}

export type AgentStatus = "draft" | "published" | "suspended";

export interface Agent {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  instructions: string | null;
  languages: string[];
  default_language: string | null;
  stt_provider: string | null;
  stt_model: string | null;
  llm_provider: string | null;
  llm_model: string | null;
  tts_provider: string | null;
  tts_model: string | null;
  voice_id: string | null;
  status: AgentStatus;
  created_at: string;
  updated_at: string;
}

export type AgentPatch = Partial<
  Pick<
    Agent,
    | "name"
    | "description"
    | "instructions"
    | "languages"
    | "default_language"
    | "stt_provider"
    | "stt_model"
    | "llm_provider"
    | "llm_model"
    | "tts_provider"
    | "tts_model"
    | "voice_id"
  >
>;

export type AnswerType =
  | "boolean"
  | "text"
  | "number"
  | "currency"
  | "enum"
  | "message"
  | "outcome";
export type WordingMode = "exact" | "flexible";
export type ValidationOperator = "eq" | "ne" | "gt" | "gte" | "lt" | "lte" | "in";

export interface BranchCondition {
  field_key: string | null;
  operator: ValidationOperator | null;
  operand: JsonValue;
}

export interface ValidationRule {
  operator: ValidationOperator | null;
  operand: JsonValue;
}

export interface ValidationFailureTarget {
  target_step_id?: string | null;
  outcome?: string | null;
}

export interface StepBranch {
  conditions: BranchCondition[];
  target_step_id?: string | null;
  outcome?: string | null;
}

export interface StepInput {
  field_key: string | null;
  title: string;
  objective: string;
  required: boolean;
  enabled: boolean;
  answer_type: AnswerType | null;
  enum_values: string[];
  wording_mode: WordingMode | null;
  localized_phrases: Record<string, string>;
  extraction_instructions: string;
  clarification_instructions: string;
  validation_rules: ValidationRule[];
  branches: StepBranch[];
  validation_failure_target: ValidationFailureTarget | null;
}

export interface AgentStep extends StepInput {
  id: string;
  agent_id: string;
  sort_order: number;
}

export type CallType = "test" | "public";
export type SessionStatus = "created" | "active" | "completed" | "failed" | "cancelled" | "expired";

export interface CallSession {
  id: string;
  agent_id: string;
  call_type: CallType;
  status: SessionStatus;
  outcome: string | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
}

export interface CallSessionGrant {
  session_id: string;
  signaling_token: string;
}

export interface CallMessage {
  id: string;
  sequence: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export interface CallAnswer {
  id: string;
  field_key: string;
  value: JsonValue;
  accepted: boolean;
  validation_result: JsonValue;
  created_at: string;
}

export interface TraceSpan {
  name: string;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  status: string;
  error_type: string | null;
}

export interface SessionTrace {
  session_id: string;
  spans: TraceSpan[];
}

export interface ProviderCatalogModel {
  id: string;
  available: boolean;
}

export interface ProviderCatalogVoice {
  id: string;
  name?: string | null;
  languages: string[];
  available: boolean;
  sample_url?: string | null;
}
export interface ProviderCatalog {
  openai: {
    available: boolean;
    error: string | null;
    models: ProviderCatalogModel[];
  };
  tts_providers: Array<{
    provider: string;
    available: boolean;
    error: string | null;
    sample_url?: string | null;
    models: Array<{
      id: string;
      available: boolean;
      voices: ProviderCatalogVoice[];
    }>;
  }>;
}
