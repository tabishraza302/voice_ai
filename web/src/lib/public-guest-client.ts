export interface PublicDeploymentBootstrap {
  public_id: string;
  agent_name: string;
  agent_description: string;
  disclosure_version: string;
  disclosure_text: string;
  csrf_token: string;
}

export interface GuestAdmission {
  session_id: string;
  state: string;
  signaling_url: string;
  join_token: string;
  join_expires_at: string;
  hard_end_deadline: string;
  csrf_token: string;
}

export interface GuestReconnectToken {
  session_id: string;
  state: string;
  signaling_url: string;
  join_token: string;
  expires_at: string;
}

export interface GuestSessionStatus {
  session_id: string;
  state: string;
  session_version: number;
  created_at: string;
  hard_end_deadline: string;
  ended_at: string | null;
}

export interface GuestSessionEnd {
  session_id: string;
  state: string;
  session_version: number;
  ended_at: string | null;
  failure_code: string | null;
}

export class PublicGuestApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "PublicGuestApiError";
  }
}

const DEFAULT_PLATFORM_API_URL =
  process.env.NEXT_PUBLIC_PLATFORM_API_URL || "/api/v1";

export class PublicGuestClient {
  private readonly baseUrl: string;

  constructor(
    baseUrl: string = DEFAULT_PLATFORM_API_URL,
    private readonly fetcher: typeof fetch = fetch,
  ) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  bootstrap(publicId: string): Promise<PublicDeploymentBootstrap> {
    return this.request<PublicDeploymentBootstrap>(
      `/public/deployments/${encodeURIComponent(publicId)}`,
    );
  }

  admit(
    publicId: string,
    bootstrap: Pick<PublicDeploymentBootstrap, "disclosure_version" | "csrf_token">,
    idempotencyKey: string,
  ): Promise<GuestAdmission> {
    return this.request<GuestAdmission>(
      `/public/deployments/${encodeURIComponent(publicId)}/sessions`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Public-CSRF": bootstrap.csrf_token,
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify({
          disclosure_version: bootstrap.disclosure_version,
          disclosure_acknowledged: true,
        }),
      },
    );
  }

  status(sessionId: string): Promise<GuestSessionStatus> {
    return this.request<GuestSessionStatus>(
      `/guest/sessions/${encodeURIComponent(sessionId)}/status`,
    );
  }

  reconnect(
    sessionId: string,
    csrfToken: string,
    idempotencyKey: string,
  ): Promise<GuestReconnectToken> {
    return this.request<GuestReconnectToken>(
      `/guest/sessions/${encodeURIComponent(sessionId)}/reconnect-token`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Guest-CSRF": csrfToken,
          "Idempotency-Key": idempotencyKey,
        },
        body: "{}",
      },
    );
  }

  end(
    sessionId: string,
    csrfToken: string,
    idempotencyKey: string,
  ): Promise<GuestSessionEnd> {
    return this.request<GuestSessionEnd>(
      `/guest/sessions/${encodeURIComponent(sessionId)}/end`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Guest-CSRF": csrfToken,
          "Idempotency-Key": idempotencyKey,
        },
        body: "{}",
      },
    );
  }

  private async request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
    const response = await this.fetcher.call(
      globalThis,
      `${this.baseUrl}${endpoint}`,
      {
        ...options,
        credentials: "include",
        cache: "no-store",
      },
    );
    if (!response.ok) {
      let code = "UNKNOWN_ERROR";
      let message = response.statusText || `Request failed with status ${response.status}`;
      try {
        const body: unknown = await response.json();
        if (body && typeof body === "object" && "error" in body) {
          const error = body.error;
          if (error && typeof error === "object") {
            if ("code" in error && typeof error.code === "string") code = error.code;
            if ("message" in error && typeof error.message === "string") {
              message = error.message;
            }
          }
        }
      } catch {
        // Keep the status-based message when the server response is not JSON.
      }
      throw new PublicGuestApiError(response.status, code, message);
    }
    return (await response.json()) as T;
  }
}
