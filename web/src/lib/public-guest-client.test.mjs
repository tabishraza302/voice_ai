import { describe, expect, test } from "bun:test";
import { PublicGuestApiError, PublicGuestClient } from "./public-guest-client.ts";

const API_URL = "https://platform.example/api/v1";

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function createClient(handler = async () => jsonResponse({})) {
  const calls = [];
  const fetcher = async (input, init = {}) => {
    calls.push({ url: String(input), init });
    return handler(input, init);
  };
  return { client: new PublicGuestClient(API_URL, fetcher), calls };
}

describe("PublicGuestClient", () => {
  test("loads public deployment disclosure without owner CSRF", async () => {
    const bootstrap = {
      public_id: "p".repeat(32),
      agent_name: "Guest agent",
      agent_description: "Public demo",
      disclosure_version: "2026-06-01",
      disclosure_text: "Public voice session disclosure.",
      csrf_token: "public-csrf",
    };
    const { client, calls } = createClient(async () => jsonResponse(bootstrap));

    await client.bootstrap(bootstrap.public_id);

    expect(calls[0].url).toBe(`${API_URL}/public/deployments/${bootstrap.public_id}`);
    expect(calls[0].init.method ?? "GET").toBe("GET");
    expect(calls[0].init.credentials).toBe("include");
    expect(calls[0].init.cache).toBe("no-store");
    expect(new Headers(calls[0].init.headers).has("X-CSRF-Token")).toBe(false);
  });

  test("admits only acknowledged disclosure with deployment CSRF and idempotency", async () => {
    const { client, calls } = createClient();

    await client.admit(
      "public-id",
      { disclosure_version: "v2", csrf_token: "public-csrf" },
      "admit-operation-1",
    );

    expect(calls[0].url).toBe(`${API_URL}/public/deployments/public-id/sessions`);
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.credentials).toBe("include");
    expect(calls[0].init.cache).toBe("no-store");
    const headers = new Headers(calls[0].init.headers);
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("X-Public-CSRF")).toBe("public-csrf");
    expect(headers.get("Idempotency-Key")).toBe("admit-operation-1");
    expect(headers.has("X-CSRF-Token")).toBe(false);
    expect(JSON.parse(calls[0].init.body)).toEqual({
      disclosure_version: "v2",
      disclosure_acknowledged: true,
    });
  });

  test("uses only the canonical guest status, reconnect-token and end paths", async () => {
    const { client, calls } = createClient(async (_input, init) =>
      jsonResponse(init.method === "GET" ? { state: "ACTIVE" } : { state: "ENDING" }, init.method === "POST" ? 202 : 200),
    );

    await client.status("session-1");
    await client.reconnect("session-1", "guest-csrf", "reconnect-operation");
    const ended = await client.end("session-1", "guest-csrf", "end-operation");

    expect(calls.map((call) => call.url)).toEqual([
      `${API_URL}/guest/sessions/session-1/status`,
      `${API_URL}/guest/sessions/session-1/reconnect-token`,
      `${API_URL}/guest/sessions/session-1/end`,
    ]);
    expect(calls[0].init.method ?? "GET").toBe("GET");
    for (const call of calls) {
      expect(call.init.credentials).toBe("include");
      expect(call.init.cache).toBe("no-store");
    }
    for (const call of calls.slice(1)) {
      const headers = new Headers(call.init.headers);
      expect(headers.get("X-Guest-CSRF")).toBe("guest-csrf");
      expect(headers.get("Idempotency-Key")).toBe(
        call.url.endsWith("reconnect-token") ? "reconnect-operation" : "end-operation",
      );
      expect(headers.has("X-CSRF-Token")).toBe(false);
      expect(JSON.parse(call.init.body)).toEqual({});
    }
    expect(ended.state).toBe("ENDING");
  });

  test("keeps API errors useful to the public page", async () => {
    const { client } = createClient(async () => jsonResponse({
      error: { code: "RATE_LIMITED", message: "Try again later." },
    }, 429));

    await expect(client.bootstrap("public-id")).rejects.toMatchObject({
      name: "PublicGuestApiError",
      status: 429,
      code: "RATE_LIMITED",
      message: "Try again later.",
    });
    await expect(client.bootstrap("public-id")).rejects.toBeInstanceOf(PublicGuestApiError);
  });
});
