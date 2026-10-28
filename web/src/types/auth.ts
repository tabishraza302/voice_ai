/**
 * Exact wire models and types for the first-release platform authentication API.
 * Defined in docs/first-release/auth-contract.md.
 */

export type UserRole = "user" | "admin";
export type UserStatus = "active" | "suspended";
export type PreferredLanguage = "en" | "hi";

export interface UserView {
  id: string; // UUID
  email: string;
  display_name: string;
  role: UserRole;
  status: UserStatus;
  email_verified_at: string; // UTC ISO string
  preferred_language: PreferredLanguage;
  timezone: string;
  low_balance_notifications: boolean;
  mfa_enabled: boolean;
  created_at: string; // UTC ISO string
  updated_at: string; // UTC ISO string
}

export type SessionState =
  | "anonymous"
  | "mfa_required"
  | "mfa_enrollment_required"
  | "authenticated";

export interface SessionResponse {
  state: SessionState;
  user: UserView | null;
  session_id: string; // UUID
  expires_at: string; // UTC ISO string
  csrf_token: string;
  step_up_expires_at: string | null; // UTC ISO string
}

export interface Accepted {
  status: "accepted";
}

export interface SessionItem {
  id: string; // UUID
  current: boolean;
  user_agent_label: string;
  created_at: string; // UTC ISO string
  last_seen_at: string; // UTC ISO string
  expires_at: string; // UTC ISO string
}

export interface SessionList {
  items: SessionItem[];
}

export interface MfaSetup {
  secret: string;
  provisioning_uri: string;
  expires_at: string; // UTC ISO string
}

export interface UserPage {
  items: UserView[];
  next_cursor: string | null;
}

export interface ApiFieldError {
  field: string;
  code: string;
}

export interface ApiErrorDetail {
  code: string;
  message: string;
  request_id: string;
  retryable: boolean;
  fields: ApiFieldError[];
}

export interface ApiErrorResponse {
  error: ApiErrorDetail;
}

// Request payloads
export interface RegisterRequest {
  email: string;
  display_name: string;
}

export interface VerifyEmailRequest {
  token: string;
  password: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface ResetPasswordRequest {
  email: string;
}

export interface ResetPasswordConfirmRequest {
  token: string;
  password: string;
}

export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

export interface MfaEnrollRequest {
  current_password: string;
}

export interface MfaConfirmRequest {
  code: string;
}

export interface MfaVerifyRequest {
  code: string;
}

export interface StepUpRequest {
  password: string;
  code: string;
}

export interface UpdateMeRequest {
  display_name?: string;
  preferred_language?: PreferredLanguage;
  timezone?: string;
  low_balance_notifications?: boolean;
}

export interface AdminSuspendRequest {
  reason: string;
}

export interface AdminRestoreRequest {
  reason: string;
}

/**
 * Validate password against contract rules:
 * - 15–128 Unicode codepoints
 * - UTF-8 encoded length at most 512 bytes
 * - Accept spaces and Unicode without trimming, normalization, or truncation
 */
export function validatePassword(password: string): { valid: boolean; error?: string } {
  if (typeof password !== "string") {
    return { valid: false, error: "Password must be a string" };
  }
  // Count Unicode codepoints accurately using spread iterator
  const codepoints = [...password].length;
  if (codepoints < 15) {
    return { valid: false, error: "Password must be at least 15 characters long" };
  }
  if (codepoints > 128) {
    return { valid: false, error: "Password must be at most 128 characters long" };
  }
  // Check UTF-8 byte length (at most 512 bytes)
  const byteLength = new TextEncoder().encode(password).length;
  if (byteLength > 512) {
    return { valid: false, error: "Password encoded length cannot exceed 512 bytes" };
  }
  return { valid: true };
}
