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

export class PlatformApiClient {
  private baseUrl: string;
  // The CSRF token is kept ONLY in memory; never written to localStorage or cookies.
  private csrfToken: string | null = null;
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
    const url = `${this.baseUrl}${endpoint.startsWith("/") ? endpoint : `/${endpoint}`}`;
    const method = (options?.method || "GET").toUpperCase();
    const isMutation = ["POST", "PUT", "PATCH", "DELETE"].includes(method);

    const headers = new Headers(options?.headers || {});

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

      throw new PlatformApiError(
        response.status,
        code,
        message,
        requestId,
        retryable,
        fieldErrors
      );
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
    return this.request<SessionResponse>("/auth/session", {
      method: "GET",
    });
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
}

export const platformApi = new PlatformApiClient();
