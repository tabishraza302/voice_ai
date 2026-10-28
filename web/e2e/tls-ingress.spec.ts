import { test, expect } from "@playwright/test";
import {
  createAdminUser,
  generateTotpCode,
  getLatestMail,
  makeAdminSessionStepUpStale,
} from "./fixtures/auth";

test.describe("TLS Production Ingress Real Authentication Journeys", () => {
  // Test 1: Production Set-Cookie Semantics and Same-Origin /api/v1 Routing
  test("Production Set-Cookie Semantics: __Host- prefix, Secure, SameSite=Lax, no Domain, and same-origin routing", async ({
    page,
    baseURL,
  }) => {
    expect(baseURL).toBeTruthy();
    expect(baseURL!).toMatch(/^https:\/\//);

    // 1. Visit root over HTTPS and await async auth bootstrap response
    await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes("/api/v1/auth/session") && res.status() === 200
      ),
      page.goto("/"),
    ]);

    // 2. Extract and inspect cookies stored in browser context
    await expect.poll(async () => {
      const cList = await page.context().cookies();
      return cList.find((c) => c.name === "__Host-platform_session");
    }).toBeDefined();

    const cookies = await page.context().cookies();
    const sessionCookie = cookies.find((c) => c.name === "__Host-platform_session");

    expect(sessionCookie).toBeDefined();
    // Verify production cookie contract according to auth-contract.md lines 106-107:
    // "__Host-platform_session=<opaque>; Path=/; HttpOnly; Secure; SameSite=Lax; no Domain"
    expect(sessionCookie!.secure).toBe(true);
    expect(sessionCookie!.httpOnly).toBe(true);
    expect(sessionCookie!.sameSite.toLowerCase()).toBe("lax");
    expect(sessionCookie!.path).toBe("/");
    // RFC 6265bis: __Host- cookies must not specify a Domain attribute (host-only)
    expect(sessionCookie!.domain).toBe("127.0.0.1");

    // 3. Verify same-origin /api/v1 routing directly via browser fetch
    const sessionData = await page.evaluate(async () => {
      const res = await fetch("/api/v1/auth/session");
      return { status: res.status, data: await res.json() };
    });
    expect(sessionData.status).toBe(200);
    expect(sessionData.data.state).toBe("anonymous");
    expect(sessionData.data.csrf_token).toBeTruthy();
  });

  // Test 2: User Registration, Mail Fragment Token, Password Setup, and Authenticated Session
  test("Registration and Mail Fragment Token Journey over HTTPS", async ({
    page,
    baseURL,
  }) => {
    const email = `tls-user-${Date.now()}@example.com`;

    // 1. Visit signup page over HTTPS
    await page.goto("/register");
    await expect(page.getByRole("heading", { name: "Create an account" })).toBeVisible();

    await page.getByLabel("Email address").fill(email);
    await page.getByLabel("Display name").fill("TLS E2E User");
    await page.getByRole("button", { name: "Continue with Email" }).click();

    // 2. 202 Accepted response screen
    await expect(page.getByRole("heading", { name: "Check your inbox" })).toBeVisible();
    await expect(page.getByText(email)).toBeVisible();

    // 3. Read deterministic file mail dispatched by platform
    const mail = getLatestMail({ email, purpose: "register", maxWaitMs: 8000 });
    expect(mail.token).toBeTruthy();
    expect(mail.purpose).toBe("register");

    // 4. Visit verification URL with token fragment over HTTPS
    await page.goto(`/verify-email#token=${mail.token}`);

    // Verify token fragment is stripped immediately from the browser URL bar
    await expect.poll(() => page.url()).not.toContain("#token=");

    // 5. Complete password setup
    await expect(page.getByRole("heading", { name: "Set your password" })).toBeVisible();
    await page.getByLabel("New password").fill("SecurePassword12345");
    await page.getByLabel("Confirm password").fill("SecurePassword12345");
    await page.getByRole("button", { name: "Create Account & Set Password" }).click();

    // 6. Verify confirmation screen
    await expect(page.getByRole("heading", { name: "Account created!" })).toBeVisible();

    // 7. Sign in with the newly created credentials
    await page.goto("/login");
    await page.getByLabel("Email address").fill(email);
    await page.getByLabel("Password").fill("SecurePassword12345");
    await page.getByRole("button", { name: "Sign in" }).click();

    await page.waitForURL((url) => url.pathname === "/" || url.pathname === "/settings");

    // 8. Verify browser cookie is updated and retains __Host- Secure semantics
    const cookies = await page.context().cookies();
    const sessionCookie = cookies.find((c) => c.name === "__Host-platform_session");
    expect(sessionCookie).toBeDefined();
    expect(sessionCookie!.secure).toBe(true);
    expect(sessionCookie!.httpOnly).toBe(true);
  });

  // Test 3: Real Logout Revokes Session and Expires Production Secure Cookie
  test("Real Logout Journey: Revokes Session and Expires __Host- Secure Cookie", async ({
    page,
    baseURL,
  }) => {
    const email = `logout-user-${Date.now()}@example.com`;

    // 1. Fast register and login
    await page.goto("/register");
    await page.getByLabel("Email address").fill(email);
    await page.getByLabel("Display name").fill("Logout User");
    await page.getByRole("button", { name: "Continue with Email" }).click();

    const mail = getLatestMail({ email, purpose: "register", maxWaitMs: 8000 });
    await page.goto(`/verify-email#token=${mail.token}`);
    await page.getByLabel("New password").fill("LogoutPassword12345");
    await page.getByLabel("Confirm password").fill("LogoutPassword12345");
    await page.getByRole("button", { name: "Create Account & Set Password" }).click();
    await expect(page.getByRole("heading", { name: "Account created!" })).toBeVisible();

    await page.goto("/login");
    await page.getByLabel("Email address").fill(email);
    await page.getByLabel("Password").fill("LogoutPassword12345");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL((url) => url.pathname === "/" || url.pathname === "/settings");

    // 2. Navigate to settings and capture authenticated session cookie
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "Account Settings" })).toBeVisible();

    const cookiesBefore = await page.context().cookies();
    const originalAuthCookie = cookiesBefore.find((c) => c.name === "__Host-platform_session");
    expect(originalAuthCookie).toBeDefined();
    const originalAuthSecret = originalAuthCookie!.value;

    const [logoutResponse] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes("/api/v1/auth/logout") && res.request().method() === "POST"
      ),
      page.getByRole("button", { name: "Sign out" }).click(),
    ]);
    expect([200, 204]).toContain(logoutResponse.status());

    // Inspect actual Set-Cookie header using headersArray()
    const headersArray = await logoutResponse.headersArray();
    const setCookieHeaders = headersArray
      .filter((h) => h.name.toLowerCase() === "set-cookie")
      .map((h) => h.value);
    const combinedSetCookies = setCookieHeaders.join("; ");
    expect(combinedSetCookies).toContain("__Host-platform_session=");
    expect(combinedSetCookies.toLowerCase()).toMatch(/max-age=0|expires=thu, 01 jan 1970/);

    await page.waitForURL((url) => url.pathname === "/login");

    // 3. Verify in-browser session has returned to anonymous state
    const sessionData = await page.evaluate(async () => {
      const res = await fetch("/api/v1/auth/session");
      return await res.json();
    });
    expect(sessionData.state).toBe("anonymous");
    expect(sessionData.user).toBeNull();

    // 4. Verify original authenticated session was invalidated on the server
    if (baseURL) {
      const probeRes = await page.request.get(`${baseURL}/api/v1/auth/session`, {
        headers: {
          Cookie: `__Host-platform_session=${originalAuthSecret}`,
        },
      });
      const probeBody = await probeRes.json();
      expect(probeBody.user).toBeNull();
      expect(probeBody.state).toBe("anonymous");
    }

    // 5. Verify protected settings route redirects back to login
    await page.goto("/settings");
    await page.waitForURL((url) => url.pathname.includes("/login"));
  });

  // Test 4: Password Reset Journey with Real File Mail and Token Fragment
  test("Password Reset Journey: Requests reset, strips fragment token, resets password, and verifies new login", async ({
    page,
  }) => {
    const email = `reset-user-${Date.now()}@example.com`;

    // Register user
    await page.goto("/register");
    await page.getByLabel("Email address").fill(email);
    await page.getByLabel("Display name").fill("Reset User");
    await page.getByRole("button", { name: "Continue with Email" }).click();

    const registerMail = getLatestMail({ email, purpose: "register", maxWaitMs: 8000 });
    await page.goto(`/verify-email#token=${registerMail.token}`);
    await page.getByLabel("New password").fill("InitialPassword12345");
    await page.getByLabel("Confirm password").fill("InitialPassword12345");
    await page.getByRole("button", { name: "Create Account & Set Password" }).click();
    await expect(page.getByRole("heading", { name: "Account created!" })).toBeVisible();

    // Request reset
    await page.goto("/forgot-password");
    await expect(page.getByRole("heading", { name: "Reset your password" })).toBeVisible();
    await page.getByLabel("Email address").fill(email);
    await page.getByRole("button", { name: "Send Reset Link" }).click();

    await expect(page.getByRole("heading", { name: "Check your inbox" })).toBeVisible();

    // Read reset email
    const resetMail = getLatestMail({ email, purpose: "password_reset", maxWaitMs: 8000 });
    expect(resetMail.token).toBeTruthy();

    // Consume reset token fragment over HTTPS
    await page.goto(`/reset-password#token=${resetMail.token}`);

    // Verify fragment is removed immediately
    await expect.poll(() => page.url()).not.toContain("#token=");

    // Set new password
    await expect(page.getByRole("heading", { name: "Choose a new password" })).toBeVisible();
    await page.getByLabel("New password", { exact: true }).fill("NewResetPassword12345");
    await page.getByLabel("Confirm new password").fill("NewResetPassword12345");
    await page.getByRole("button", { name: "Reset Password" }).click();

    await expect(page.getByRole("heading", { name: "Password updated" })).toBeVisible();

    // Sign in with new password via confirmation link
    await page.getByRole("link", { name: "Sign in with new password" }).click();
    await page.waitForURL("**/login");
    await page.getByLabel("Email address").fill(email);
    await page.getByLabel("Password").fill("NewResetPassword12345");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL((url) => url.pathname === "/" || url.pathname === "/settings");
  });

  // Test 5: Admin MFA Enrollment, Stale Step-Up Interception, and Privileged Mutation
  test("Admin MFA Enrollment and Stale Step-Up Flow with Real TOTP Verification", async ({
    page,
  }) => {
    const adminEmail = `admin-tls-${Date.now()}@example.com`;
    const adminPassword = "AdminSecurePassword12345";
    const targetUserEmail = `target-user-${Date.now()}@example.com`;

    // 1. Seed admin and a regular user directly in PostgreSQL
    createAdminUser({
      email: adminEmail,
      displayName: "TLS Admin Operator",
      password: adminPassword,
    });

    // Fast-register a second user to suspend
    await page.goto("/register");
    await page.getByLabel("Email address").fill(targetUserEmail);
    await page.getByLabel("Display name").fill("Target Suspension User");
    await page.getByRole("button", { name: "Continue with Email" }).click();
    const mail = getLatestMail({ email: targetUserEmail, purpose: "register", maxWaitMs: 8000 });
    await page.goto(`/verify-email#token=${mail.token}`);
    await page.getByLabel("New password").fill("TargetPassword12345");
    await page.getByLabel("Confirm password").fill("TargetPassword12345");
    await page.getByRole("button", { name: "Create Account & Set Password" }).click();
    await expect(page.getByRole("heading", { name: "Account created!" })).toBeVisible();

    // 2. Sign in as admin over HTTPS
    await page.goto("/login");
    await page.getByLabel("Email address").fill(adminEmail);
    await page.getByLabel("Password").fill(adminPassword);
    await page.getByRole("button", { name: "Sign in" }).click();

    // Admin without active MFA is redirected to MFA enrollment
    await page.waitForURL("**/admin/settings");
    await expect(page.getByRole("heading", { name: "Administrator Security" })).toBeVisible();

    // Begin enrollment
    await page.getByLabel("Current admin password").fill(adminPassword);
    await page.getByRole("button", { name: "Begin MFA Enrollment" }).click();

    // Extract secret and generate real RFC 6238 TOTP code
    const secretInput = page.getByLabel("TOTP Secret Key (Base32)");
    await expect(secretInput).toBeVisible();
    const secret = await secretInput.inputValue();
    expect(secret).toBeTruthy();

    const totpCode = generateTotpCode(secret);
    await page.getByLabel("Step 3: Enter the generated 6-digit code").fill(totpCode);
    await page.getByRole("button", { name: "Confirm & Enable MFA" }).click();

    const mfaSection = page.locator('section[aria-labelledby="mfa-section-heading"]');
    await expect(mfaSection.getByText("Active", { exact: true })).toBeVisible();

    // 3. Force admin session step-up timestamp to be STALE immediately after enrollment
    makeAdminSessionStepUpStale({ email: adminEmail });

    // 4. Navigate to User Management
    await page.goto("/admin/users");
    await expect(page.getByRole("heading", { name: "User Management" })).toBeVisible();

    // 5. Locate the target user row and attempt privileged action (Suspend)
    await page.getByPlaceholder("Search by email or display name...").fill(targetUserEmail);
    await page.getByRole("button", { name: "Search" }).click();

    const targetRow = page.locator("table tbody tr").filter({ hasText: targetUserEmail });
    await expect(targetRow).toBeVisible();

    await targetRow.getByRole("button", { name: "Suspend" }).click();
    await expect(page.getByRole("heading", { name: "Suspend Account" })).toBeVisible();
    await page.getByLabel(/Audit reason/).fill("Audit suspension to test stale step-up interception flow");
    await page.getByRole("button", { name: "Confirm Suspension" }).click();

    // 6. The server returns 403 STEP_UP_REQUIRED on stale step-up;
    // verify the UI intercepts it and opens the StepUpDialog!
    const stepUpDialog = page.getByRole("dialog").filter({ hasText: "Privileged Step-Up Required" });
    await expect(stepUpDialog).toBeVisible();
    await expect(page.getByText("Confirm your admin credentials to proceed with this sensitive action.")).toBeVisible();

    // 7. Resolve step-up inside the dialog using admin password and valid next TOTP code (+1)
    const stepUpTotp = generateTotpCode(secret, 1);
    await stepUpDialog.locator("#step-up-password").fill(adminPassword);
    await stepUpDialog.locator("#step-up-code").fill(stepUpTotp);
    await stepUpDialog.getByRole("button", { name: "Verify & Proceed" }).click();

    // 8. After resolution, the pending suspension automatically completes and dialog closes
    await expect(stepUpDialog).not.toBeVisible();
    // Verify target user status is now suspended
    await expect(targetRow.locator("td").filter({ hasText: "suspended" })).toBeVisible();

    // 9. Test last-admin safety under privileged operation
    await page.getByPlaceholder("Search by email or display name...").fill(adminEmail);
    await page.getByRole("button", { name: "Search" }).click();
    const adminRow = page.locator("table tbody tr").filter({ hasText: adminEmail });
    await expect(adminRow).toBeVisible();

    await adminRow.getByRole("button", { name: "Suspend" }).click();
    await page.getByLabel(/Audit reason/).fill("Attempting to suspend self to verify last admin refusal");
    await page.getByRole("button", { name: "Confirm Suspension" }).click();

    await expect(
      page.getByRole("alert").filter({ hasText: "Cannot suspend the last active administrator" })
    ).toBeVisible();
    await page.getByRole("button", { name: "Cancel" }).click();
  });

  // Test 6: Bad Origin Protection Fails Closed on Mutations
  test("Bad Origin Enforcement: Invalid origins cannot mutate state and return 403 ORIGIN_FORBIDDEN", async ({
    page,
    baseURL,
  }) => {
    expect(baseURL).toBeTruthy();

    // 1. Attacker domain origin
    const badOriginRes = await page.request.post(`${baseURL}/api/v1/auth/login`, {
      headers: {
        Origin: "https://attacker.example.com",
        "Content-Type": "application/json",
      },
      data: {
        email: "nonexistent@example.com",
        password: "WrongPassword12345",
      },
    });
    expect(badOriginRes.status()).toBe(403);
    const badOriginBody = await badOriginRes.json();
    expect(badOriginBody.error.code).toBe("ORIGIN_FORBIDDEN");

    // 2. Null origin (simulates sandbox/data-uri requests)
    const nullOriginRes = await page.request.post(`${baseURL}/api/v1/auth/login`, {
      headers: {
        Origin: "null",
        "Content-Type": "application/json",
      },
      data: {
        email: "nonexistent@example.com",
        password: "WrongPassword12345",
      },
    });
    expect(nullOriginRes.status()).toBe(403);
    const nullOriginBody = await nullOriginRes.json();
    expect(nullOriginBody.error.code).toBe("ORIGIN_FORBIDDEN");

    // 3. Foreign port on loopback
    const badPortRes = await page.request.post(`${baseURL}/api/v1/auth/login`, {
      headers: {
        Origin: "https://127.0.0.1:9999",
        "Content-Type": "application/json",
      },
      data: {
        email: "nonexistent@example.com",
        password: "WrongPassword12345",
      },
    });
    expect(badPortRes.status()).toBe(403);
    const badPortBody = await badPortRes.json();
    expect(badPortBody.error.code).toBe("ORIGIN_FORBIDDEN");

    // 4. In-browser fetch attempt from an untrusted document origin (data: URI)
    await page.goto("data:text/html,<html><body>cross-site-test</body></html>");
    const browserCrossSiteRes = await page.evaluate(async (apiUrl) => {
      try {
        const r = await fetch(`${apiUrl}/api/v1/auth/login`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            email: "test@example.com",
            password: "WrongPassword12345",
          }),
        });
        return { status: r.status, ok: r.ok };
      } catch (err) {
        return { error: String(err) };
      }
    }, baseURL);

    // Browser either blocks request or server responds with 403
    if ("status" in browserCrossSiteRes) {
      expect(browserCrossSiteRes.status).toBe(403);
    } else {
      expect(browserCrossSiteRes.error).toBeTruthy();
    }

    // 5. Bootstrap session & CSRF token to verify matching Origin passes origin guard
    const sessionRes = await page.request.get(`${baseURL}/api/v1/auth/session`, {
      headers: { Origin: baseURL! },
    });
    expect(sessionRes.status()).toBe(200);
    const sessionBody = await sessionRes.json();
    const csrfToken = sessionBody.csrf_token;
    expect(csrfToken).toBeTruthy();

    // Matching origin with valid CSRF reaches authentication logic
    const validOriginRes = await page.request.post(`${baseURL}/api/v1/auth/login`, {
      headers: {
        Origin: baseURL!,
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      data: {
        email: "unknown-user@example.com",
        password: "WrongPassword12345",
      },
    });
    // Valid origin is NOT rejected with 403 ORIGIN_FORBIDDEN
    expect(validOriginRes.status()).toBe(401);
    const validOriginBody = await validOriginRes.json();
    expect(validOriginBody.error.code).toBe("INVALID_CREDENTIALS");
  });
});
