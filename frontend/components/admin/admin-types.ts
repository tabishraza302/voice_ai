export type AdminRole = "user" | "admin";

export type AdminSession = {
  email: string;
  role: AdminRole;
};

export type GlobalPrompt = {
  prompt_text: string;
  version: number;
};

export type AdminPolicy = {
  max_call_duration_seconds: number;
  max_concurrent_calls_per_agent: number;
  max_concurrent_calls_overall: number;
};

export type AdminUser = {
  id: string;
  email: string;
  role: AdminRole;
  created_at: string;
  agent_count: number;
  session_count: number;
  active_session_count: number;
};

export type AdminAgentStatus = "draft" | "published" | "suspended";

export type AdminAgent = {
  id: string;
  owner_id: string;
  owner_email: string;
  name: string;
  slug: string;
  status: AdminAgentStatus;
  created_at: string;
  updated_at: string;
  step_count: number;
  session_count: number;
  active_session_count: number;
};

export type AdminAgentSuspension = {
  id: string;
  status: "suspended";
  updated_at: string;
};
