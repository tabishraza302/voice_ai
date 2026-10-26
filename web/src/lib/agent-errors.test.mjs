import { afterEach, describe, expect, test } from "bun:test";
import { ApiClient } from "./api-client.ts";
import { describeAgentLoadError } from "./agent-errors.ts";

const MISSING_AGENT_DETAIL = "This agent no longer exists. It may have been deleted.";
const NO_RESPONSE_DETAIL = "The API did not return a response.";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("describeAgentLoadError", () => {
  test("a 404 from the API client reports the agent as missing", () => {
    expect(
      describeAgentLoadError(
        new Error('API Error [404]: {"detail":"Agent not found"}'),
      ),
    ).toEqual({ notFound: true, detail: MISSING_AGENT_DETAIL });
  });

  test("a bare 404 message still reports the agent as missing", () => {
    expect(describeAgentLoadError(new Error("API Error [404]: Not Found"))).toEqual({
      notFound: true,
      detail: MISSING_AGENT_DETAIL,
    });
  });

  test("a generic Error keeps its message as the detail", () => {
    expect(
      describeAgentLoadError(new Error("API Error [500]: database is unavailable")),
    ).toEqual({
      notFound: false,
      detail: "API Error [500]: database is unavailable",
    });
  });

  test("a TypeError keeps its message as the detail", () => {
    expect(describeAgentLoadError(new TypeError("Failed to fetch"))).toEqual({
      notFound: false,
      detail: "Failed to fetch",
    });
  });

  test("a non-404 error is not treated as not found even when it mentions 404", () => {
    expect(describeAgentLoadError(new Error("API Error [502]: upstream 404"))).toEqual({
      notFound: false,
      detail: "API Error [502]: upstream 404",
    });
  });

  test("trims whitespace around an Error message", () => {
    expect(describeAgentLoadError(new Error("   Failed to fetch\n"))).toEqual({
      notFound: false,
      detail: "Failed to fetch",
    });
  });

  test("an Error with an empty message falls back to the no-response copy", () => {
    expect(describeAgentLoadError(new Error(""))).toEqual({
      notFound: false,
      detail: NO_RESPONSE_DETAIL,
    });
    expect(describeAgentLoadError(new Error("   \n\t "))).toEqual({
      notFound: false,
      detail: NO_RESPONSE_DETAIL,
    });
  });

  test("null and undefined are stringified, never thrown", () => {
    expect(describeAgentLoadError(null)).toEqual({ notFound: false, detail: "null" });
    expect(describeAgentLoadError(undefined)).toEqual({
      notFound: false,
      detail: "undefined",
    });
  });

  test("a plain string is used as the detail", () => {
    expect(describeAgentLoadError("Network request failed")).toEqual({
      notFound: false,
      detail: "Network request failed",
    });
  });

  test("a blank non-Error value falls back to the no-response copy", () => {
    expect(describeAgentLoadError("")).toEqual({ notFound: false, detail: NO_RESPONSE_DETAIL });
    expect(describeAgentLoadError("   ")).toEqual({
      notFound: false,
      detail: NO_RESPONSE_DETAIL,
    });
  });

  test("a value that cannot be stringified still yields a detail", () => {
    const hostile = {
      toString() {
        throw new Error("no string for you");
      },
    };
    expect(describeAgentLoadError(hostile)).toEqual({
      notFound: false,
      detail: NO_RESPONSE_DETAIL,
    });
  });

  test("never throws for any input", () => {
    const inputs = [null, undefined, 0, 42, false, true, "", "boom", [], {}, new Error("x")];
    for (const input of inputs) {
      expect(() => describeAgentLoadError(input)).not.toThrow();
    }
  });

  test("maps a real ApiClient 404 and a real transport failure", async () => {
    const client = new ApiClient("http://voice-agent.test/api");
    const restoreFetch = globalThis.fetch;

    try {
      globalThis.fetch = async () =>
        new Response(JSON.stringify({ detail: "Agent not found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        });
      const notFoundError = await client.getAgent("gone").catch((err) => err);
      expect(describeAgentLoadError(notFoundError)).toEqual({
        notFound: true,
        detail: MISSING_AGENT_DETAIL,
      });

      globalThis.fetch = async () => {
        throw new TypeError("Failed to fetch");
      };
      const transportError = await client.getAgent("agent-1").catch((err) => err);
      expect(describeAgentLoadError(transportError)).toEqual({
        notFound: false,
        detail: "Failed to fetch",
      });
    } finally {
      // Restore here as well as in afterEach: another test file in the same
      // process must never inherit the throwing mock, even if this test throws.
      globalThis.fetch = restoreFetch;
    }
  });
});
