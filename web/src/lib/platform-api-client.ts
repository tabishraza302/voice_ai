import type {
  Accepted,
  AdminRestoreRequest,
  AdminSuspendRequest,
  ApiErrorResponse,
  ApiFieldError,
  ChangePasswordRequest,
  LoginRequest,
  MfaConfirmRequest,
  MfaEnrollRequest,
  MfaSetup,
  MfaVerifyRequest,
  RegisterRequest,
  ResetPasswordConfirmRequest,
  ResetPasswordRequest,
  SessionList,
  SessionResponse,
  StepUpRequest,
  UpdateMeRequest,
  UserPage,
  UserStatus,
  UserView,
  VerifyEmailRequest,
} from "@/types/auth";
import type {
  AdminAdmissionPausePage,
  AdmissionPauseRequest,
  AdminAgentItem,
  AdminAgentDetail,
  AdminAgentFilters,
  AdminAgentPage,
  AdminAuditPage,
  AdminOverview,
  AdminSessionFilters,
  AdminSessionDetail,
  AdminSessionPage,
  AdminTariffPage,
  AdminTariffVersionItem,
  AdminTranscriptCorrectionItem,
  AdminTranscriptPolicyView,
  ConfigSnapshotView,
  OwnerSessionsPage,
  OwnerUsagePage,
  SessionFilters,
  SessionStateView,
  TranscriptPageView,
  TariffVersionRequest,
  WalletEntriesPage,
  WalletView,
} from "@/types/platform";

export class PlatformApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;
  readonly retryable: boolean;
  readonly fieldErrors: ApiFieldError[];

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string = "",
    retryable: boolean = false,
    fieldErrors: ApiFieldError[] = []
  ) {
    super(message);
    this.name = "PlatformApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.retryable = retryable;
    this.fieldErrors = fieldErrors;
  }
}

const DEFAULT_PLATFORM_API_URL =
  process.env.NEXT_PUBLIC_PLATFORM_API_URL || "/api/v1";

function buildQuery(
  params: Record<string, string | number | boolean | null | undefined>
): string {
  const searchParams = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      searchParams.set(key, String(value));
    }
  }
  const query = searchParams.toString();
  return query ? `?${query}` : "";
}

export class PlatformApiClient {
  private baseUrl: string;
  // The CSRF token is kept ONLY in memory; never written to localStorage or cookies.
  private csrfToken: string | null = null;
  private sessionBootstrap: Promise<SessionResponse> | null = null;
  private sessionListeners = new Set<(session: SessionResponse | null) => void>();

  constructor(baseUrl: string = DEFAULT_PLATFORM_API_URL) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  getBaseUrl(): string {
    return this.baseUrl;
  }

  getCsrfToken(): string | null {
    return this.csrfToken;
  }

  setCsrfToken(token: string | null): void {
    this.csrfToken = token;
  }

  onSessionChange(listener: (session: SessionResponse | null) => void): () => void {
    this.sessionListeners.add(listener);
    return () => {
      this.sessionListeners.delete(listener);
    };
  }

  notifySessionChange(session: SessionResponse | null): void {
    for (const listener of this.sessionListeners) {
      try {
        listener(session);
      } catch {
        // Prevent listener error from breaking client flow
      }
    }
  }

  async request<T>(endpoint: string, options?: RequestInit): Promise<T> {
    return this.requestOnce<T>(endpoint, options, true);
  }

  /**
   * Obtain the anonymous session exactly once while a bootstrap is in flight.
   *
   * Mutations need both the session cookie and its in-memory CSRF token. If a
   * page submits a form before the auth provider's bootstrap returns, the POST
   * would otherwise be rejected as CSRF_INVALID. Sharing one in-flight GET
   * also keeps the provider and the first mutation from creating two sessions.
   */
  private bootstrapSession(): Promise<SessionResponse> {
    if (!this.sessionBootstrap) {
      this.sessionBootstrap = this.requestOnce<SessionResponse>(
        "/auth/session",
        { method: "GET" },
        false
      ).finally(() => {
        this.sessionBootstrap = null;
      });
    }
    return this.sessionBootstrap;
  }

  private async requestOnce<T>(
    endpoint: string,
    options: RequestInit | undefined,
    allowCsrfRetry: boolean
  ): Promise<T> {
    const url = `${this.baseUrl}${endpoint.startsWith("/") ? endpoint : `/${endpoint}`}`;
    const method = (options?.method || "GET").toUpperCase();
    const isMutation = ["POST", "PUT", "PATCH", "DELETE"].includes(method);

    // A mutation cannot be signed without the session it belongs to. Wait for
    // the shared bootstrap; if it fails, the request still proceeds and fails
    // closed with the server's own error.
    if (isMutation && !this.csrfToken) {
      try {
        await this.bootstrapSession();
      } catch {
        // Keep the original request path; CSRF_INVALID is the honest failure.
      }
    }

    const headers = new Headers(options?.headers || {});
    // Only requests that carry the server's replay-dedup key may be replayed.
    // A non-idempotent mutation (for example a user suspend) must fail closed:
    // the existing contract forbids replaying mutations without that key.
    const replaySafe = headers.has("Idempotency-Key");

    // Ensure credentials: 'include' on every platform call
    const fetchOptions: RequestInit = {
      ...options,
      method,
      credentials: "include",
    };

    // For mutations, attach CSRF token if present in memory, and enforce application/json Content-Type
    if (isMutation) {
      if (this.csrfToken && !headers.has("X-CSRF-Token")) {
        headers.set("X-CSRF-Token", this.csrfToken);
      }
      if (!headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
      }
    } else {
      // Safe methods (GET, HEAD, OPTIONS) MUST NOT include X-CSRF-Token
      headers.delete("X-CSRF-Token");
    }

    fetchOptions.headers = headers;

    // The token actually sent, which is what a CSRF rejection refers to.
    const sentCsrfToken = headers.get("X-CSRF-Token");

    const response = await fetch(url, fetchOptions);

    // 204 No Content (or 205) must not parse JSON
    if (response.status === 204 || response.status === 205) {
      return undefined as unknown as T;
    }

    const contentType = response.headers.get("content-type") || "";
    const isJson = contentType.includes("application/json");

    if (!response.ok) {
      let code = "UNKNOWN_ERROR";
      let message = response.statusText || `Request failed with status ${response.status}`;
      let requestId = response.headers.get("x-request-id") || "";
      let retryable = response.status === 429 || response.status === 503;
      let fieldErrors: ApiFieldError[] = [];

      if (isJson) {
        try {
          const errorBody = (await response.json()) as ApiErrorResponse;
          if (errorBody?.error) {
            code = errorBody.error.code || code;
            message = errorBody.error.message || message;
            requestId = errorBody.error.request_id || requestId;
            retryable = errorBody.error.retryable ?? retryable;
            fieldErrors = errorBody.error.fields || [];
          }
        } catch {
          // Keep fallback error details
        }
      } else {
        try {
          const text = await response.text();
          if (text) message = text;
        } catch {
          // Keep statusText
        }
      }

      const apiError = new PlatformApiError(
        response.status,
        code,
        message,
        requestId,
        retryable,
        fieldErrors
      );

      // A stale in-memory CSRF token (for example after the server rotated the
      // session) is recoverable exactly once: refresh the session, then replay
      // the mutation with the new token. Mutations are protected by their
      // Idempotency-Key, so a replay cannot double-apply.
      if (
        allowCsrfRetry &&
        isMutation &&
        replaySafe &&
        response.status === 403 &&
        code === "CSRF_INVALID" &&
        sentCsrfToken
      ) {
        this.csrfToken = null;
        try {
          await this.bootstrapSession();
        } catch {
          throw apiError;
        }
        return this.requestOnce<T>(endpoint, options, false);
      }

      throw apiError;
    }

    if (!isJson) {
      return undefined as unknown as T;
    }

    const data = await response.json();

    // If the response is a SessionResponse, update the in-memory CSRF token and notify listeners
    if (
      data &&
      typeof data === "object" &&
      "csrf_token" in data &&
      "state" in data
    ) {
      const session = data as SessionResponse;
      this.csrfToken = session.csrf_token;
      this.notifySessionChange(session);
    }

    return data as T;
  }

  // --- Auth lifecycle endpoints ---

  async getSession(): Promise<SessionResponse> {
    return this.bootstrapSession();
  }

  async register(data: RegisterRequest): Promise<Accepted> {
    return this.request<Accepted>("/auth/register", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async verifyEmail(data: VerifyEmailRequest): Promise<UserView> {
    return this.request<UserView>("/auth/verify-email", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async login(data: LoginRequest): Promise<SessionResponse> {
    return this.request<SessionResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async logout(): Promise<void> {
    try {
      await this.request<void>("/auth/logout", {
        method: "POST",
        body: JSON.stringify({}),
      });
    } finally {
      this.csrfToken = null;
      this.notifySessionChange(null);
    }
  }

  async logoutAll(): Promise<void> {
    try {
      await this.request<void>("/auth/logout-all", {
        method: "POST",
        body: JSON.stringify({}),
      });
    } finally {
      this.csrfToken = null;
      this.notifySessionChange(null);
    }
  }

  async requestPasswordReset(data: ResetPasswordRequest): Promise<Accepted> {
    return this.request<Accepted>("/auth/password/reset-request", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async confirmPasswordReset(data: ResetPasswordConfirmRequest): Promise<void> {
    return this.request<void>("/auth/password/reset-confirm", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async changePassword(data: ChangePasswordRequest): Promise<SessionResponse> {
    return this.request<SessionResponse>("/auth/password/change", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async getSessions(): Promise<SessionList> {
    return this.request<SessionList>("/auth/sessions", {
      method: "GET",
    });
  }

  async revokeSession(sessionId: string): Promise<void> {
    return this.request<void>(`/auth/sessions/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    });
  }

  // --- MFA and Step-Up ---

  async enrollMfa(data: MfaEnrollRequest): Promise<MfaSetup> {
    return this.request<MfaSetup>("/auth/mfa/enroll", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async confirmMfa(data: MfaConfirmRequest): Promise<SessionResponse> {
    return this.request<SessionResponse>("/auth/mfa/confirm", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async verifyMfa(data: MfaVerifyRequest): Promise<SessionResponse> {
    return this.request<SessionResponse>("/auth/mfa/verify", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async stepUp(data: StepUpRequest): Promise<SessionResponse> {
    return this.request<SessionResponse>("/auth/step-up", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  // --- User profile & settings ---

  async getMe(): Promise<UserView> {
    return this.request<UserView>("/me", {
      method: "GET",
    });
  }

  async updateMe(data: UpdateMeRequest): Promise<UserView> {
    return this.request<UserView>("/me", {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  }

  // --- Admin endpoints ---

  async adminGetUsers(params?: {
    limit?: number;
    cursor?: string | null;
    q?: string;
    status?: UserStatus;
  }): Promise<UserPage> {
    const searchParams = new URLSearchParams();
    if (params?.limit) searchParams.set("limit", String(params.limit));
    if (params?.cursor) searchParams.set("cursor", params.cursor);
    if (params?.q) searchParams.set("q", params.q);
    if (params?.status) searchParams.set("status", params.status);

    const query = searchParams.toString();
    const endpoint = query ? `/admin/users?${query}` : "/admin/users";
    return this.request<UserPage>(endpoint, {
      method: "GET",
    });
  }

  async adminGetUser(userId: string): Promise<UserView> {
    return this.request<UserView>(`/admin/users/${encodeURIComponent(userId)}`, {
      method: "GET",
    });
  }

  async adminSuspendUser(
    userId: string,
    data: AdminSuspendRequest
  ): Promise<UserView> {
    return this.request<UserView>(
      `/admin/users/${encodeURIComponent(userId)}/suspend`,
      {
        method: "POST",
        body: JSON.stringify(data),
      }
    );
  }

  async adminRestoreUser(
    userId: string,
    data: AdminRestoreRequest
  ): Promise<UserView> {
    return this.request<UserView>(
      `/admin/users/${encodeURIComponent(userId)}/restore`,
      {
        method: "POST",
        body: JSON.stringify(data),
      }
    );
  }
  async getWallet(): Promise<WalletView> {
    return this.request<WalletView>("/wallet", { method: "GET" });
  }

  async getWalletEntries(params?: {
    cursor?: string | null;
    limit?: number;
  }): Promise<WalletEntriesPage> {
    return this.request<WalletEntriesPage>(
      `/wallet/entries${buildQuery({ cursor: params?.cursor, limit: params?.limit })}`,
      { method: "GET" }
    );
  }

  async getOwnerSessions(params?: SessionFilters): Promise<OwnerSessionsPage> {
    return this.request<OwnerSessionsPage>(
      `/sessions${buildQuery({
        cursor: params?.cursor,
        limit: params?.limit,
        state: params?.state,
        kind: params?.kind,
        agent_version_id: params?.agent_version_id,
        from_at: params?.from_at,
        to_at: params?.to_at,
      })}`,
      { method: "GET" }
    );
  }

  async getOwnerUsage(params?: SessionFilters): Promise<OwnerUsagePage> {
    return this.request<OwnerUsagePage>(
      `/usage${buildQuery({
        cursor: params?.cursor,
        limit: params?.limit,
        state: params?.state,
        kind: params?.kind,
        agent_version_id: params?.agent_version_id,
        from_at: params?.from_at,
        to_at: params?.to_at,
      })}`,
      { method: "GET" }
    );
  }

  async getOwnerConfigSnapshot(sessionId: string): Promise<ConfigSnapshotView> {
    return this.request<ConfigSnapshotView>(
      `/sessions/${encodeURIComponent(sessionId)}/config-snapshot`,
      { method: "GET" }
    );
  }

  async getOwnerTranscript(
    sessionId: string,
    params?: { cursor?: string | null; limit?: number }
  ): Promise<TranscriptPageView> {
    return this.request<TranscriptPageView>(
      `/sessions/${encodeURIComponent(sessionId)}/transcript${buildQuery({
        cursor: params?.cursor,
        limit: params?.limit,
      })}`,
      { method: "GET" }
    );
  }

  async adminGetOverview(): Promise<AdminOverview> {
    return this.request<AdminOverview>("/admin/overview", { method: "GET" });
  }

  async adminGetSessions(params?: AdminSessionFilters): Promise<AdminSessionPage> {
    return this.request<AdminSessionPage>(
      `/admin/sessions${buildQuery({
        cursor: params?.cursor,
        limit: params?.limit,
        owner_user_id: params?.owner_user_id,
        state: params?.state,
        kind: params?.kind,
        agent_version_id: params?.agent_version_id,
        from_at: params?.from_at,
        to_at: params?.to_at,
      })}`,
      { method: "GET" }
    );
  }

  async adminGetSession(sessionId: string): Promise<AdminSessionDetail> {
    return this.request<AdminSessionDetail>(
      `/admin/sessions/${encodeURIComponent(sessionId)}`,
      { method: "GET" }
    );
  }

  async adminGetWallet(userId: string): Promise<WalletView> {
    return this.request<WalletView>(
      `/admin/users/${encodeURIComponent(userId)}/wallet`,
      { method: "GET" }
    );
  }

  async adminGetWalletEntries(
    userId: string,
    params?: { cursor?: string | null; limit?: number }
  ): Promise<WalletEntriesPage> {
    return this.request<WalletEntriesPage>(
      `/admin/users/${encodeURIComponent(userId)}/wallet/entries${buildQuery({
        cursor: params?.cursor,
        limit: params?.limit,
      })}`,
      { method: "GET" }
    );
  }

  async adminGetAgents(params?: AdminAgentFilters): Promise<AdminAgentPage> {
    return this.request<AdminAgentPage>(
      `/admin/agents${buildQuery({
        cursor: params?.cursor,
        limit: params?.limit,
        owner_user_id: params?.owner_user_id,
        q: params?.q,
        enabled: params?.enabled,
      })}`,
      { method: "GET" }
    );
  }

  async adminGetAgent(agentId: string): Promise<AdminAgentDetail> {
    return this.request<AdminAgentDetail>(
      `/admin/agents/${encodeURIComponent(agentId)}`,
      { method: "GET" }
    );
  }

  async adminDisableAgent(
    agentId: string,
    reason: string,
    idempotencyKey: string
  ): Promise<AdminAgentItem> {
    return this.request<AdminAgentItem>(
      `/admin/agents/${encodeURIComponent(agentId)}/disable`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify({ reason }),
      }
    );
  }

  async adminGetTariffs(params?: {
    cursor?: string | null;
    limit?: number;
  }): Promise<AdminTariffPage> {
    return this.request<AdminTariffPage>(
      `/admin/tariffs${buildQuery({
        cursor: params?.cursor,
        limit: params?.limit,
      })}`,
      { method: "GET" }
    );
  }

  async adminCreateTariffVersion(
    data: TariffVersionRequest,
    idempotencyKey: string
  ): Promise<AdminTariffVersionItem> {
    return this.request<AdminTariffVersionItem>("/admin/tariffs/versions", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(data),
    });
  }

  async adminGetAdmissionPauses(params?: {
    scope?: "global" | "user" | "agent";
    owner_user_id?: string;
    agent_id?: string;
    cursor?: string | null;
    limit?: number;
  }): Promise<AdminAdmissionPausePage> {
    return this.request<AdminAdmissionPausePage>(
      `/admin/admission-pauses${buildQuery({
        scope: params?.scope,
        owner_user_id: params?.owner_user_id,
        agent_id: params?.agent_id,
        cursor: params?.cursor,
        limit: params?.limit,
      })}`,
      { method: "GET" }
    );
  }

  async adminSetAdmissionPause(
    data: AdmissionPauseRequest,
    idempotencyKey: string
  ): Promise<AdminAdmissionPausePage["items"][number]> {
    return this.request<AdminAdmissionPausePage["items"][number]>(
      "/admin/admission-pauses",
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify(data),
      }
    );
  }

  async adminAccessTranscript(
    sessionId: string,
    data: { reason: string; cursor?: string | null; limit?: number },
    idempotencyKey: string
  ): Promise<TranscriptPageView> {
    return this.request<TranscriptPageView>(
      `/admin/sessions/${encodeURIComponent(sessionId)}/transcript-access`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify(data),
      }
    );
  }

  async adminCreateTranscriptCorrection(
    sessionId: string,
    data: {
      event_id: string;
      expected_revision: number;
      replacement_text: string;
      reason: string;
    },
    idempotencyKey: string
  ): Promise<AdminTranscriptCorrectionItem> {
    return this.request<AdminTranscriptCorrectionItem>(
      `/admin/sessions/${encodeURIComponent(sessionId)}/transcript-corrections`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify(data),
      }
    );
  }

  async adminSetTranscriptVisibility(
    sessionId: string,
    data: { visible_to_owner: boolean; expected_revision: number; reason: string },
    idempotencyKey: string
  ): Promise<AdminTranscriptPolicyView> {
    return this.request<AdminTranscriptPolicyView>(
      `/admin/sessions/${encodeURIComponent(sessionId)}/transcript-visibility`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify(data),
      }
    );
  }

  async adminTerminateSession(
    sessionId: string,
    reason: string,
    idempotencyKey: string
  ): Promise<SessionStateView> {
    return this.request<SessionStateView>(
      `/admin/sessions/${encodeURIComponent(sessionId)}/terminate`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify({ reason }),
      }
    );
  }

  async adminGetAuditLogs(params?: {
    actor_id?: string;
    action?: string;
    target_type?: string;
    target_id?: string;
    from_at?: string;
    to_at?: string;
    cursor?: string | null;
    limit?: number;
  }): Promise<AdminAuditPage> {
    return this.request<AdminAuditPage>(
      `/admin/audit-logs${buildQuery({
        actor_id: params?.actor_id,
        action: params?.action,
        target_type: params?.target_type,
        target_id: params?.target_id,
        from_at: params?.from_at,
        to_at: params?.to_at,
        cursor: params?.cursor,
        limit: params?.limit,
      })}`,
      { method: "GET" }
    );
  }
}

export const platformApi = new PlatformApiClient();
