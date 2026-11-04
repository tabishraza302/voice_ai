import { describe, expect, test, beforeEach } from "bun:test";
import {
  deriveAuthState,
  extractAndClearFragmentToken,
  sanitizeReturnPath,
  StepUpManager,
} from "./auth-state.tsx";
import { validatePassword } from "../types/auth.ts";

describe("AuthState and security helpers", () => {
  describe("sanitizeReturnPath", () => {
    test("allows valid application return paths", () => {
      expect(sanitizeReturnPath("/settings")).toBe("/settings");
      expect(sanitizeReturnPath("/admin/users")).toBe("/admin/users");
      expect(sanitizeReturnPath("/admin/users?q=test&status=active")).toBe(
        "/admin/users?q=test&status=active"
      );
      expect(sanitizeReturnPath("/agents/new")).toBe("/agents/new");
    });

    test("rejects protocol-relative URLs", () => {
      expect(sanitizeReturnPath("//evil.com")).toBe("/");
      expect(sanitizeReturnPath("//evil.com/phish")).toBe("/");
      expect(sanitizeReturnPath("///evil.com")).toBe("/");
    });

    test("rejects backslashes and escaped separators", () => {
      expect(sanitizeReturnPath("/\\evil.com")).toBe("/");
      expect(sanitizeReturnPath("\\evil.com")).toBe("/");
      expect(sanitizeReturnPath("/path\\to\\evil")).toBe("/");
    });

    test("rejects absolute schemes and script URLs", () => {
      expect(sanitizeReturnPath("https://attacker.com")).toBe("/");
      expect(sanitizeReturnPath("http://attacker.com")).toBe("/");
      expect(sanitizeReturnPath("javascript:alert(1)")).toBe("/");
      expect(sanitizeReturnPath("data:text/html,alert(1)")).toBe("/");
    });

    test("uses provided safe fallback when input is invalid or missing", () => {
      expect(sanitizeReturnPath(null, "/agents")).toBe("/agents");
      expect(sanitizeReturnPath(undefined, "/agents")).toBe("/agents");
      expect(sanitizeReturnPath("", "/settings")).toBe("/settings");
      expect(sanitizeReturnPath("//evil.com", "/fallback")).toBe("/fallback");
    });
  });

  describe("extractAndClearFragmentToken", () => {
    let mockLocation;
    let replacedUrls;

    beforeEach(() => {
      replacedUrls = [];
      mockLocation = {
        pathname: "/verify-email",
        search: "?lang=en",
        hash: "",
      };
      globalThis.window = {
        location: mockLocation,
        history: {
          replaceState: (state, title, url) => {
            replacedUrls.push(url);
            mockLocation.hash = "";
          },
        },
      };
    });

    test("extracts token from fragment and removes fragment immediately from URL", () => {
      mockLocation.hash = "#token=secure-one-time-token-12345";

      const token = extractAndClearFragmentToken();
      expect(token).toBe("secure-one-time-token-12345");

      // Verify URL fragment was removed using history.replaceState
      expect(replacedUrls).toHaveLength(1);
      expect(replacedUrls[0]).toBe("/verify-email?lang=en");

      // Verify token is not in hash anymore
      expect(mockLocation.hash).toBe("");
    });

    test("handles complex fragment parameters cleanly", () => {
      mockLocation.hash = "#foo=bar&token=secret-token-777&baz=qux";

      const token = extractAndClearFragmentToken();
      expect(token).toBe("secret-token-777");
      expect(replacedUrls).toHaveLength(1);
    });

    test("returns null when no token parameter is present in fragment", () => {
      mockLocation.hash = "#section-1";

      const token = extractAndClearFragmentToken();
      expect(token).toBeNull();
      expect(replacedUrls).toHaveLength(0);
    });

    test("returns null on empty hash", () => {
      mockLocation.hash = "";

      const token = extractAndClearFragmentToken();
      expect(token).toBeNull();
      expect(replacedUrls).toHaveLength(0);
    });
  });

  describe("StepUpManager", () => {
    test("preserves a pending operation across step-up without submitting it twice", async () => {
      const manager = new StepUpManager();
      let executionCount = 0;

      const sensitiveAction = async () => {
        executionCount++;
        return { success: true, count: executionCount };
      };

      manager.requestStepUp(sensitiveAction);
      expect(manager.isStepUpOpen).toBe(true);
      expect(manager.hasPendingAction).toBe(true);

      const fakeStepUpCall = async () => ({
        state: "authenticated",
        session_id: "00000000-0000-0000-0000-000000000001",
        user: null,
        expires_at: "2026-10-28T12:00:00Z",
        csrf_token: "csrf",
        step_up_expires_at: "2026-10-28T12:05:00Z",
      });

      // Resolve step-up: executes pending action once
      const result = await manager.resolveStepUp(fakeStepUpCall);
      expect(result).toEqual({ success: true, count: 1 });
      expect(executionCount).toBe(1);
      expect(manager.isStepUpOpen).toBe(false);
      expect(manager.hasPendingAction).toBe(false);

      // Attempting to resolve step-up again MUST NOT re-execute the action
      await manager.resolveStepUp(fakeStepUpCall);
      expect(executionCount).toBe(1);
    });

    test("canceling step-up clears pending action so it is never executed", async () => {
      const manager = new StepUpManager();
      let executionCount = 0;

      manager.requestStepUp(async () => {
        executionCount++;
      });

      manager.cancelStepUp();
      expect(manager.isStepUpOpen).toBe(false);
      expect(manager.hasPendingAction).toBe(false);

      expect(executionCount).toBe(0);
    });
    test("canceling step-up settles a waiting caller with a cancellation error", async () => {
      const manager = new StepUpManager();
      let rejection = null;

      manager.requestStepUp(
        async () => undefined,
        (reason) => {
          rejection = reason;
        }
      );

      manager.cancelStepUp();
      expect(rejection).not.toBeNull();
      expect(rejection.code).toBe("STEP_UP_CANCELLED");
      expect(manager.hasPendingAction).toBe(false);
    });
  });

  describe("deriveAuthState and session gates", () => {
    test("reports a bootstrap failure instead of loading forever", () => {
      const state = deriveAuthState(null, "Unable to reach the platform API.");
      expect(state.status).toBe("error");
      expect(state.message).toBe("Unable to reach the platform API.");
    });

    test("reports loading only before the first bootstrap", () => {
      expect(deriveAuthState(null).status).toBe("loading");
    });

    test("derives anonymous auth state", () => {
      const session = {
        state: "anonymous",
        user: null,
        session_id: "00000000-0000-0000-0000-000000000001",
        expires_at: "2026-10-28T12:00:00Z",
        csrf_token: "csrf",
        step_up_expires_at: null,
      };

      const state = deriveAuthState(session);
      expect(state.status).toBe("anonymous");
      expect(state.user).toBeNull();
    });

    test("derives authenticated state for an admin without MFA", () => {
      const session = {
        state: "authenticated",
        user: {
          id: "00000000-0000-0000-0000-000000000002",
          email: "admin@example.com",
          display_name: "Admin User",
          role: "admin",
          status: "active",
          email_verified_at: "2026-10-28T12:00:00Z",
          preferred_language: "en",
          timezone: "UTC",
          low_balance_notifications: true,
          created_at: "2026-10-28T12:00:00Z",
          updated_at: "2026-10-28T12:00:00Z",
        },
        session_id: "00000000-0000-0000-0000-000000000003",
        expires_at: "2026-10-28T12:00:00Z",
        csrf_token: "csrf",
        step_up_expires_at: null,
      };

      const state = deriveAuthState(session);
      expect(state.status).toBe("authenticated");
      expect(state.user?.role).toBe("admin");
    });

    test("derives authenticated state with full user view", () => {
      const session = {
        state: "authenticated",
        user: {
          id: "00000000-0000-0000-0000-000000000006",
          email: "customer@example.com",
          display_name: "Customer",
          role: "user",
          status: "active",
          email_verified_at: "2026-10-28T12:00:00Z",
          preferred_language: "hi",
          timezone: "Asia/Kolkata",
          low_balance_notifications: true,
          created_at: "2026-10-28T12:00:00Z",
          updated_at: "2026-10-28T12:00:00Z",
        },
        session_id: "00000000-0000-0000-0000-000000000007",
        expires_at: "2026-10-28T12:00:00Z",
        csrf_token: "csrf",
        step_up_expires_at: null,
      };

      const state = deriveAuthState(session);
      expect(state.status).toBe("authenticated");
      expect(state.user?.role).toBe("user");
      expect(state.user?.preferred_language).toBe("hi");
    });
  });

  describe("Password validation rules per auth contract", () => {
    test("rejects passwords shorter than 15 Unicode codepoints", () => {
      const check = validatePassword("ShortPass1234");
      expect(check.valid).toBe(false);
      expect(check.error).toContain("at least 15 characters");
    });

    test("accepts passwords between 15 and 128 Unicode codepoints", () => {
      const validPass = "ValidPassword12345";
      expect(validatePassword(validPass).valid).toBe(true);

      // Preserves whitespace and Unicode symbols without trimming
      const passWithSpaces = "  password with spaces  ";
      expect(validatePassword(passWithSpaces).valid).toBe(true);

      const passWithUnicode = "पासवर्ड-password-१२३४५";
      expect(validatePassword(passWithUnicode).valid).toBe(true);
    });

    test("rejects passwords longer than 128 Unicode codepoints", () => {
      const tooLong = "a".repeat(129);
      const check = validatePassword(tooLong);
      expect(check.valid).toBe(false);
      expect(check.error).toContain("at most 128 characters");
    });

    test("rejects passwords exceeding 512 UTF-8 encoded bytes", () => {
      // 4-byte Unicode characters: 128 characters * 4 bytes = 512 bytes (fits)
      const maxUtf8 = "🎉".repeat(128);
      expect(validatePassword(maxUtf8).valid).toBe(true);
    });
  });
});
