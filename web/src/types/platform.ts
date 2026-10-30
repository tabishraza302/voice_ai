export type SessionKind = "voice" | "chat" | "voice_test";

export interface WalletView {
  user_id: string;
  currency: "INR";
  posted_minor: number;
  reserved_minor: number;
  available_minor: number;
  version: number;
  updated_at: string;
}

export interface WalletEntryView {
  id: string;
  user_id: string;
  kind: "credit" | "charge" | "refund" | "adjustment";
  amount_minor: number;
  session_id: string | null;
  operation_id: string;
  actor: string;
  reason: string;
  created_at: string;
}

export interface WalletEntriesPage {
  items: WalletEntryView[];
  next_cursor: string | null;
}

export interface SessionHistoryItem {
  session_id: string;
  agent_version_id: string | null;
  kind: SessionKind;
  state: string;
  session_version: number;
  connect_deadline: string | null;
  hard_end_deadline: string | null;
  created_at: string;
  ended_at: string | null;
  failure_code: string | null;
}

export interface OwnerSessionsPage {
  items: SessionHistoryItem[];
  next_cursor: string | null;
}

export interface SessionBillingView {
  budget_status: "OPEN" | "CLOSED";
  charge_status: "provisional" | "final";
  pinned_rate_minor_per_minute: number;
  billed_ms: number;
  authorized_ms: number;
  authorized_estimate_minor: number | null;
  charged_minor: number;
  reserved_minor: number;
  budget_version: number;
  waived_unknown_usage: boolean;
}

export interface OwnerUsageItem {
  session: SessionHistoryItem;
  billing: SessionBillingView | null;
}

export interface OwnerUsagePage {
  items: OwnerUsageItem[];
  next_cursor: string | null;
}

export interface ConfigSnapshotView {
  session_id: string;
  agent_id: string;
  agent_name: string;
  agent_version_id: string;
  agent_version: number;
  flow_hash: string;
  artifact_hash: string;
  authoring_hash: string;
  immutable_config: Record<string, unknown>;
  tariff_snapshot: Record<string, unknown> | null;
  published_at: string;
}

export interface TranscriptSegmentView {
  sequence: number;
  event_id: string;
  speaker: string;
  final_text: string;
  start_ms: number;
  end_ms: number;
  kind: string;
  corrected: boolean;
}

export interface TranscriptPageView {
  session_id: string;
  transcript_revision: number;
  policy_revision: number;
  visible_to_owner: boolean;
  segments: TranscriptSegmentView[];
  has_gaps: boolean;
  gaps: Array<{ after_sequence: number; before_sequence: number; reason: string }>;
  processing: boolean;
  next_cursor: string | null;
}

export interface AdminSessionItem extends SessionHistoryItem {
  owner_user_id: string;
  runner_id: string | null;
  billing: SessionBillingView | null;
}

export interface AdminSessionDetail extends AdminSessionItem {
  tariff_snapshot: Record<string, unknown> | null;
  pinned_config_snapshot: ConfigSnapshotView | null;
  transcript_visible_to_owner: boolean;
  transcript_policy_revision: number;
}

export interface AdminSessionPage {
  items: AdminSessionItem[];
  next_cursor: string | null;
}

export interface AdminAgentItem {
  id: string;
  owner_user_id: string;
  name: string;
  description: string;
  enabled: boolean;
  draft_revision: number;
  current_published_version_id: string | null;
  current_published_version: number | null;
  created_at: string;
  updated_at: string;
}

export interface AdminAgentDetail extends AdminAgentItem {
  owner_email: string;
  owner_display_name: string;
  immutable_config: Record<string, unknown> | null;
  flow_hash: string | null;
  artifact_hash: string | null;
  authoring_hash: string | null;
}

export interface AdminAgentPage {
  items: AdminAgentItem[];
  next_cursor: string | null;
}

export interface AdminTariffVersionItem {
  id: string;
  revision: number;
  tariff_version: string;
  rate_minor_per_minute: number;
  billing_slice_ms: number;
  permit_ttl_ms: number;
  is_current: boolean;
  created_at: string;
}

export interface AdminTariffPage {
  current: AdminTariffVersionItem;
  items: AdminTariffVersionItem[];
  next_cursor: string | null;
}

export interface AdminAdmissionPauseItem {
  id: string;
  scope: "global" | "user" | "agent";
  owner_user_id: string | null;
  agent_id: string | null;
  paused: boolean;
  revision: number;
  reason: string;
  updated_by_user_id: string;
  created_at: string;
  updated_at: string;
}

export interface AdminAdmissionPausePage {
  items: AdminAdmissionPauseItem[];
  next_cursor: string | null;
}

export interface AdminTranscriptCorrectionItem {
  id: string;
  session_id: string;
  event_id: string;
  policy_revision: number;
  created_at: string;
}

export interface AdminTranscriptPolicyView {
  session_id: string;
  visible_to_owner: boolean;
  policy_revision: number;
  updated_at: string | null;
}

export interface SessionStateView {
  session_id: string;
  state: string;
  session_version: number;
  ended_at: string | null;
  failure_code: string | null;
}

export interface AdminAuditItem {
  id: string;
  actor_kind: string;
  actor_id: string | null;
  session_id: string | null;
  action: string;
  target_type: string;
  target_id: string | null;
  reason: string | null;
  request_id: string | null;
  source_ip: string | null;
  result: string;
  created_at: string;
}

export interface AdminAuditPage {
  items: AdminAuditItem[];
  next_cursor: string | null;
}
export interface PageFilters {
  cursor?: string | null;
  limit?: number;
}

export interface SessionFilters extends PageFilters {
  state?: string;
  kind?: SessionKind;
  agent_version_id?: string;
  from_at?: string;
  to_at?: string;
}

export interface AdminSessionFilters extends PageFilters {
  owner_user_id?: string;
  state?: string;
  kind?: SessionKind;
  agent_version_id?: string;
  from_at?: string;
  to_at?: string;
}

export interface AdminAgentFilters extends PageFilters {
  owner_user_id?: string;
  q?: string;
  enabled?: boolean;
}


export interface AdmissionPauseRequest {
  scope: "global" | "user" | "agent";
  owner_user_id?: string | null;
  agent_id?: string | null;
  paused: boolean;
  expected_revision: number;
  reason: string;
}

export interface TariffVersionRequest {
  expected_revision: number;
  rate_minor_per_minute: number;
  reason: string;
}


export interface AdminOverview {
  generated_at: string;
  users_total: number;
  users_active: number;
  agents_total: number;
  owner_enabled_agents: number;
  sessions_total: number;
  wallet_accounts: number;
  wallet_posted_minor: number;
  wallet_reserved_minor: number;
  wallet_available_minor: number;
  call_budgets_open: number;
  call_budgets_closed: number;
  provisional_charged_minor: number;
  settled_charged_minor: number;
}
