import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { PlatformApiClient, PlatformApiError } from "./platform-api-client.ts";

const originalFetch = globalThis.fetch;

describe("PlatformApiClient", () => {
  let calls = [];
  let client;

  beforeEach(() => {
    calls = [];
    client = new PlatformApiClient("http://127.0.0.1:8000/api/v1");
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  function mockFetch(handler) {
    globalThis.fetch = async (input, init = {}) => {
      calls.push({ url: String(input), init });
      return handler(input, init);
    };
  }

  test("credentials: 'include' is present on every request", async () => {
    mockFetch(async (url, init) => {
      if (url.endsWith("/auth/session")) {
        return new Response(
          JSON.stringify({
            state: "anonymous",
            user: null,
            session_id: "00000000-0000-0000-0000-000000000001",
            expires_at: "2026-10-28T12:00:00Z",
            csrf_token: "csrf-token-1",
            step_up_expires_at: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.endsWith("/auth/login")) {
        return new Response(
          JSON.stringify({
            state: "authenticated",
            user: {
              id: "00000000-0000-0000-0000-000000000002",
              email: "user@example.com",
              display_name: "Test User",
              role: "user",
              status: "active",
              email_verified_at: "2026-10-28T12:00:00Z",
              preferred_language: "en",
              timezone: "UTC",
              low_balance_notifications: true,
              mfa_enabled: false,
              created_at: "2026-10-28T12:00:00Z",
              updated_at: "2026-10-28T12:00:00Z",
            },
            session_id: "00000000-0000-0000-0000-000000000003",
            expires_at: "2026-10-29T12:00:00Z",
            csrf_token: "csrf-token-2",
            step_up_expires_at: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response("{}", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    await client.getSession();
    expect(calls[0].init.credentials).toBe("include");

    await client.login({ email: "user@example.com", password: "SecretPassword12345" });
    expect(calls[1].init.credentials).toBe("include");

    await client.getMe();
    expect(calls[2].init.credentials).toBe("include");
  });

  test("mutation-only CSRF header: attached to POST/PATCH/DELETE, excluded on GET", async () => {
    client.setCsrfToken("csrf-active-token");

    mockFetch(async (url, init) => {
      if (init.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return new Response(
        JSON.stringify({
          state: "authenticated",
          user: null,
          session_id: "00000000-0000-0000-0000-000000000001",
          expires_at: "2026-10-29T12:00:00Z",
          csrf_token: "csrf-active-token",
          step_up_expires_at: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    });

    // GET request must NOT have X-CSRF-Token
    await client.getSession();
    const getHeaders = calls[0].init.headers;
    expect(getHeaders.get("X-CSRF-Token")).toBeNull();

    // POST mutation MUST have X-CSRF-Token
    await client.changePassword({
      current_password: "OldPassword12345",
      new_password: "NewPassword12345",
    });
    const postHeaders = calls[1].init.headers;
    expect(postHeaders.get("X-CSRF-Token")).toBe("csrf-active-token");

    // DELETE mutation MUST have X-CSRF-Token and Content-Type: application/json
    await client.revokeSession("00000000-0000-0000-0000-000000000099");
    const deleteHeaders = calls[2].init.headers;
    expect(deleteHeaders.get("X-CSRF-Token")).toBe("csrf-active-token");
    expect(deleteHeaders.get("Content-Type")).toBe("application/json");
  });

  test("session deletion sends DELETE with application/json Content-Type, X-CSRF-Token and credentials, returning 204", async () => {
    client.setCsrfToken("csrf-session-revoke-token");

    mockFetch(async (url, init) => {
      expect(init.method).toBe("DELETE");
      expect(init.credentials).toBe("include");
      expect(init.headers.get("Content-Type")).toBe("application/json");
      expect(init.headers.get("X-CSRF-Token")).toBe("csrf-session-revoke-token");
      expect(url).toBe("http://127.0.0.1:8000/api/v1/auth/sessions/00000000-0000-0000-0000-000000000099");
      return new Response(null, { status: 204 });
    });

    const result = await client.revokeSession("00000000-0000-0000-0000-000000000099");
    expect(result).toBeUndefined();
  });

  test("token update after rotation: updates in-memory token on session response", async () => {
    expect(client.getCsrfToken()).toBeNull();

    mockFetch(async () => {
      return new Response(
        JSON.stringify({
          state: "authenticated",
          user: {
            id: "00000000-0000-0000-0000-000000000002",
            email: "admin@example.com",
            display_name: "Admin",
            role: "admin",
            status: "active",
            email_verified_at: "2026-10-28T12:00:00Z",
            preferred_language: "en",
            timezone: "UTC",
            low_balance_notifications: true,
            mfa_enabled: true,
            created_at: "2026-10-28T12:00:00Z",
            updated_at: "2026-10-28T12:00:00Z",
          },
          session_id: "00000000-0000-0000-0000-000000000005",
          expires_at: "2026-10-29T12:00:00Z",
          csrf_token: "rotated-token-987",
          step_up_expires_at: "2026-10-28T12:05:00Z",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    });

    const session = await client.login({
      email: "admin@example.com",
      password: "MasterPassword12345",
    });

    expect(session.csrf_token).toBe("rotated-token-987");
    expect(client.getCsrfToken()).toBe("rotated-token-987");
  });

  test("204 No Content completes without calling response.json()", async () => {
    mockFetch(async () => {
      return new Response(null, {
        status: 204,
        statusText: "No Content",
      });
    });

    const result = await client.logout();
    expect(result).toBeUndefined();
    expect(client.getCsrfToken()).toBeNull();
  });

  test("exact typed error code, field errors, request ID, and retryable flag", async () => {
    mockFetch(async () => {
      return new Response(
        JSON.stringify({
          error: {
            code: "VALIDATION",
            message: "New password does not meet minimum length",
            request_id: "00000000-0000-0000-0000-000000000077",
            retryable: false,
            fields: [{ field: "new_password", code: "TOO_SHORT" }],
          },
        }),
        {
          status: 422,
          headers: {
            "Content-Type": "application/json",
            "X-Request-ID": "00000000-0000-0000-0000-000000000077",
          },
        }
      );
    });

    let caughtError = null;
    try {
      await client.changePassword({
        current_password: "CurrentPassword12345",
        new_password: "short",
      });
    } catch (err) {
      caughtError = err;
    }

    expect(caughtError).toBeInstanceOf(PlatformApiError);
    expect(caughtError.status).toBe(422);
    expect(caughtError.code).toBe("VALIDATION");
    expect(caughtError.message).toBe("New password does not meet minimum length");
    expect(caughtError.requestId).toBe("00000000-0000-0000-0000-000000000077");
    expect(caughtError.retryable).toBe(false);
    expect(caughtError.fieldErrors).toEqual([
      { field: "new_password", code: "TOO_SHORT" },
    ]);
  });

  test("session notifications deliver state changes and null on logout", async () => {
    const delivered = [];
    const unsubscribe = client.onSessionChange((session) => {
      delivered.push(session ? session.state : null);
    });

    mockFetch(async (url, init) => {
      if (url.endsWith("/auth/login")) {
        return new Response(
          JSON.stringify({
            state: "authenticated",
            user: null,
            session_id: "00000000-0000-0000-0000-000000000001",
            expires_at: "2026-10-29T12:00:00Z",
            csrf_token: "tok-1",
            step_up_expires_at: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.endsWith("/auth/logout")) {
        return new Response(null, { status: 204 });
      }
      return new Response("{}", { status: 200 });
    });

    await client.login({ email: "user@example.com", password: "Password12345678" });
    expect(delivered).toEqual(["authenticated"]);

    await client.logout();
    expect(delivered).toEqual(["authenticated", null]);

    unsubscribe();
    await client.login({ email: "user@example.com", password: "Password12345678" });
    // Should not receive further notifications after unsubscribe
    expect(delivered).toEqual(["authenticated", null]);
  });

  test("no storage of secrets: localStorage and sessionStorage remain untouched", async () => {
    const fakeLocalStorage = {};
    const fakeSessionStorage = {};
    globalThis.localStorage = {
      setItem: (k, v) => { fakeLocalStorage[k] = v; },
      getItem: (k) => fakeLocalStorage[k] || null,
      removeItem: (k) => { delete fakeLocalStorage[k]; },
      clear: () => { Object.keys(fakeLocalStorage).forEach(k => delete fakeLocalStorage[k]); },
    };
    globalThis.sessionStorage = {
      setItem: (k, v) => { fakeSessionStorage[k] = v; },
      getItem: (k) => fakeSessionStorage[k] || null,
      removeItem: (k) => { delete fakeSessionStorage[k]; },
      clear: () => { Object.keys(fakeSessionStorage).forEach(k => delete fakeSessionStorage[k]); },
    };

    mockFetch(async () => {
      return new Response(
        JSON.stringify({
          state: "authenticated",
          user: null,
          session_id: "00000000-0000-0000-0000-000000000001",
          expires_at: "2026-10-29T12:00:00Z",
          csrf_token: "secret-csrf-token",
          step_up_expires_at: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    });

    await client.login({ email: "user@example.com", password: "Password12345678" });

    expect(Object.keys(fakeLocalStorage)).toHaveLength(0);
    expect(Object.keys(fakeSessionStorage)).toHaveLength(0);
  });

  test("no automatic retry of non-idempotent operation on CSRF failure", async () => {
    let callCount = 0;
    mockFetch(async () => {
      callCount++;
      return new Response(
        JSON.stringify({
          error: {
            code: "CSRF_INVALID",
            message: "CSRF token invalid",
            request_id: "00000000-0000-0000-0000-000000000088",
            retryable: false,
            fields: [],
          },
        }),
        {
          status: 403,
          headers: { "Content-Type": "application/json" },
        }
      );
    });

    client.setCsrfToken("stale-csrf");

    let threw = false;
    try {
      await client.adminSuspendUser("00000000-0000-0000-0000-000000000002", {
        reason: "Administrative policy violation investigation",
      });
    } catch (err) {
      threw = true;
      expect(err).toBeInstanceOf(PlatformApiError);
      expect(err.code).toBe("CSRF_INVALID");
    }

    expect(threw).toBe(true);
    // Non-idempotent suspend mutation must NOT have been retried automatically
    expect(callCount).toBe(1);
  });
  test("history and admin operations keep query, CSRF, and idempotency contracts", async () => {
    client.setCsrfToken("csrf-platform-policy");
    mockFetch(async () =>
      new Response("{}", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );

    await client.getWallet();
    await client.getOwnerSessions({ state: "ACTIVE", kind: "voice_test", limit: 20 });
    await client.getOwnerUsage({ limit: 20 });
    await client.adminGetAgents({
      owner_user_id: "owner-1",
      q: "Safe Agent",
      limit: 25,
    });
    await client.adminCreateTariffVersion(
      {
        expected_revision: 1,
        rate_minor_per_minute: 9000,
        reason: "Approved price update PRC-318",
      },
      "tariff-key-1"
    );
    await client.adminSetAdmissionPause(
      {
        scope: "global",
        paused: true,
        expected_revision: 0,
        reason: "Emergency maintenance window",
      },
      "pause-key-1"
    );
    await client.adminCreateTranscriptCorrection(
      "session-1",
      {
        event_id: "event-1",
        expected_revision: 0,
        replacement_text: "Corrected transcript text",
        reason: "Customer verified transcript correction",
      },
      "correction-key-1"
    );
    await client.adminSetTranscriptVisibility(
      "session-1",
      {
        visible_to_owner: false,
        expected_revision: 1,
        reason: "Hide content pending owner review",
      },
      "visibility-key-1"
    );
    await client.adminTerminateSession(
      "session-1",
      "Verified active-call safety issue",
      "termination-key-1"
    );

    expect(new URL(calls[0].url).pathname).toBe("/api/v1/wallet");
    const sessionsUrl = new URL(calls[1].url);
    expect(sessionsUrl.pathname).toBe("/api/v1/sessions");
    expect(sessionsUrl.searchParams.get("state")).toBe("ACTIVE");
    expect(sessionsUrl.searchParams.get("kind")).toBe("voice_test");
    expect(sessionsUrl.searchParams.get("limit")).toBe("20");
    expect(new URL(calls[2].url).pathname).toBe("/api/v1/usage");
    const agentsUrl = new URL(calls[3].url);
    expect(agentsUrl.pathname).toBe("/api/v1/admin/agents");
    expect(agentsUrl.searchParams.get("q")).toBe("Safe Agent");
    expect(agentsUrl.searchParams.get("owner_user_id")).toBe("owner-1");

    const mutationPaths = [
      "/api/v1/admin/tariffs/versions",
      "/api/v1/admin/admission-pauses",
      "/api/v1/admin/sessions/session-1/transcript-corrections",
      "/api/v1/admin/sessions/session-1/transcript-visibility",
      "/api/v1/admin/sessions/session-1/terminate",
    ];
    const idempotencyKeys = [
      "tariff-key-1",
      "pause-key-1",
      "correction-key-1",
      "visibility-key-1",
      "termination-key-1",
    ];
    for (const [index, path] of mutationPaths.entries()) {
      const { url, init } = calls[index + 4];
      expect(new URL(url).pathname).toBe(path);
      expect(init.method).toBe("POST");
      expect(init.credentials).toBe("include");
      expect(init.headers.get("X-CSRF-Token")).toBe("csrf-platform-policy");
      expect(init.headers.get("Idempotency-Key")).toBe(idempotencyKeys[index]);
    }
  });
});
