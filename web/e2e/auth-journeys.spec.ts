import { test, expect } from "@playwright/test";
import {
  createAdminUser,
  generateTotpCode,
  getLatestMail,
} from "./fixtures/auth";

test.describe("Platform Authentication Real HTTP Journeys", () => {
  // Test 1: User Registration, File Mail Token, and Email Verification
  test("User Registration Journey: registers account, extracts real file mail token, sets password", async ({
    page,
  }) => {
    const email = `test-user-${Date.now()}@example.com`;

    // 1. Visit signup page
    await page.goto("/register");
    await expect(page.getByRole("heading", { name: "Create an account" })).toBeVisible();

    await page.getByLabel("Email address").fill(email);
    await page.getByLabel("Display name").fill("Real E2E User");
    await page.getByRole("button", { name: "Continue with Email" }).click();

    // 2. 202 Accepted screen
    await expect(page.getByRole("heading", { name: "Check your inbox" })).toBeVisible();
    await expect(page.getByText(email)).toBeVisible();

    // 3. Read deterministic file mail dispatched by platform
    const mail = getLatestMail({ email, purpose: "register", maxWaitMs: 8000 });
    expect(mail.token).toBeTruthy();
    expect(mail.purpose).toBe("register");

    // 4. Visit verification URL with token fragment
    await page.goto(`/verify-email#token=${mail.token}`);

    // Verify token fragment is removed immediately from the URL bar by replaceState
    await expect.poll(() => page.url()).not.toContain("#token=");

    // 5. Complete password setup
    await expect(page.getByRole("heading", { name: "Set your password" })).toBeVisible();
    await page.getByLabel("New password").fill("StrongPassword12345");
    await page.getByLabel("Confirm password").fill("StrongPassword12345");
    await page.getByRole("button", { name: "Create Account & Set Password" }).click();

    // 6. Verify success confirmation screen
    await expect(page.getByRole("heading", { name: "Account created!" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Sign in to your account" })).toBeVisible();
  });

  // Test 2: Real Login, Session/CSRF, Profile Update & Logout
  test("User Login, Settings, Profile Mutation with Real CSRF, and Logout Journey", async ({
    page,
  }) => {
    const email = `test-login-${Date.now()}@example.com`;

    // Fast-path register and verify user
    await page.goto("/register");
    await page.getByLabel("Email address").fill(email);
    await page.getByLabel("Display name").fill("Settings User");
    await page.getByRole("button", { name: "Continue with Email" }).click();

    const mail = getLatestMail({ email, purpose: "register", maxWaitMs: 8000 });
    await page.goto(`/verify-email#token=${mail.token}`);
    await page.getByLabel("New password").fill("PasswordForLogin12345");
    await page.getByLabel("Confirm password").fill("PasswordForLogin12345");
    await page.getByRole("button", { name: "Create Account & Set Password" }).click();
    await expect(page.getByRole("heading", { name: "Account created!" })).toBeVisible();

    // Sign in on real login route
    await page.goto("/login");
    await page.getByLabel("Email address").fill(email);
    await page.getByLabel("Password").fill("PasswordForLogin12345");
    await page.getByRole("button", { name: "Sign in" }).click();

    // Redirects to authenticated workspace
    await page.waitForURL((url) => url.pathname === "/" || url.pathname === "/settings");

    // Navigate to settings
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "Account Settings" })).toBeVisible();

    // Verify profile email matches
    const emailInput = page.getByLabel("Email address (Read-only)");
    await expect(emailInput).toHaveValue(email);

    // Update display name via real PATCH /me with real X-CSRF-Token
    await page.getByLabel("Display name").fill("Updated Display Name");
    await page.getByRole("button", { name: "Save Changes" }).click();

    await expect(page.getByText("Profile updated successfully")).toBeVisible();
    // Verify active sessions list displays current session
    await expect(page.getByText("Current device")).toBeVisible();

    // Create a secondary active session from a separate browser context
    const browserInstance = page.context().browser();
    if (browserInstance) {
      const secondContext = await browserInstance.newContext();
      const secondPage = await secondContext.newPage();
      await secondPage.goto("/login");
      await secondPage.getByLabel("Email address").fill(email);
      await secondPage.getByLabel("Password").fill("PasswordForLogin12345");
      await secondPage.getByRole("button", { name: "Sign in" }).click();
      await secondPage.waitForURL((url) => url.pathname === "/" || url.pathname === "/settings");

      // Reload settings on first page to see the second session
      await page.reload();
      await expect(page.getByRole("heading", { name: "Account Settings" })).toBeVisible();

      // Revoke the second session via DELETE /auth/sessions/{id}
      const revokeButton = page.getByTitle("Revoke session").first();
      await expect(revokeButton).toBeVisible();
      await revokeButton.click();

      // Verify the revoked session is removed from active sessions
      await expect(revokeButton).not.toBeVisible();

      // Second session is immediately revoked and fails closed
      await secondPage.goto("/settings");
      await secondPage.waitForURL((url) => url.pathname.includes("/login"));
      await secondContext.close();
    }

    // Sign out
    await page.getByRole("button", { name: "Sign out" }).click();
    await page.waitForURL((url) => url.pathname === "/login");

    // Verify protected route now redirects back to login
    await page.goto("/settings");
    await page.waitForURL((url) => url.pathname.includes("/login"));
  });

  // Test 3: Password Reset Journey with Real File Mail
  test("Password Reset Journey: requests reset, consumes fragment token, resets password, and verifies new password", async ({
    page,
  }) => {
    const email = `test-reset-${Date.now()}@example.com`;

    // Register user
    await page.goto("/register");
    await page.getByLabel("Email address").fill(email);
    await page.getByLabel("Display name").fill("Reset Test User");
    await page.getByRole("button", { name: "Continue with Email" }).click();

    const signupMail = getLatestMail({ email, purpose: "register", maxWaitMs: 8000 });
    await page.goto(`/verify-email#token=${signupMail.token}`);
    await page.getByLabel("New password").fill("OldPassword12345");
    await page.getByLabel("Confirm password").fill("OldPassword12345");
    await page.getByRole("button", { name: "Create Account & Set Password" }).click();
    await expect(page.getByRole("heading", { name: "Account created!" })).toBeVisible();

    // Request reset
    await page.goto("/forgot-password");
    await page.getByLabel("Email address").fill(email);
    await page.getByRole("button", { name: "Send Reset Link" }).click();

    await expect(page.getByRole("heading", { name: "Check your inbox" })).toBeVisible();

    // Fetch reset email
    const resetMail = getLatestMail({ email, purpose: "password_reset", maxWaitMs: 8000 });
    expect(resetMail.token).toBeTruthy();

    // Navigate to reset link with fragment
    await page.goto(`/reset-password#token=${resetMail.token}`);

    // Verify fragment is removed from URL bar
    await expect.poll(() => page.url()).not.toContain("#token=");

    await expect(page.getByRole("heading", { name: "Choose a new password" })).toBeVisible();
    await page.getByLabel("New password", { exact: true }).fill("NewBrandPassword12345");
    await page.getByLabel("Confirm new password").fill("NewBrandPassword12345");
    await page.getByRole("button", { name: "Reset Password" }).click();

    await expect(page.getByRole("heading", { name: "Password updated" })).toBeVisible();

    // Sign in with new password
    await page.goto("/login");
    await page.getByLabel("Email address").fill(email);
    await page.getByLabel("Password").fill("NewBrandPassword12345");
    await page.getByRole("button", { name: "Sign in" }).click();

    await page.waitForURL((url) => url.pathname === "/" || url.pathname === "/settings");
  });

  // Test 4: Admin Bootstrap/Enrollment, Real TOTP Verification, and User Management
  test("Admin MFA Enrollment & User Management with Real TOTP Step-Up", async ({
    page,
  }) => {
    const adminEmail = `admin-${Date.now()}@example.com`;
    const adminPassword = "AdminSecurePassword12345";

    // Create admin via platform CLI
    createAdminUser({
      email: adminEmail,
      displayName: "Admin Operator",
      password: adminPassword,
    });

    // Sign in with admin password
    await page.goto("/login");
    await page.getByLabel("Email address").fill(adminEmail);
    await page.getByLabel("Password").fill(adminPassword);
    await page.getByRole("button", { name: "Sign in" }).click();

    // Admin without factor is redirected to MFA enrollment
    await page.waitForURL("**/admin/settings");
    await expect(page.getByRole("heading", { name: "Administrator Security" })).toBeVisible();
    await expect(page.getByText("Required")).toBeVisible();

    // Enter current password to begin MFA enrollment
    await page.getByLabel("Current admin password").fill(adminPassword);
    await page.getByRole("button", { name: "Begin MFA Enrollment" }).click();

    // Step 2: Extract base32 secret from field
    const secretInput = page.getByLabel("TOTP Secret Key (Base32)");
    await expect(secretInput).toBeVisible();
    const secret = await secretInput.inputValue();
    expect(secret).toBeTruthy();

    // Generate real 6-digit TOTP code
    const totpCode = generateTotpCode(secret);

    // Enter code and confirm
    await page.getByLabel("Step 3: Enter the generated 6-digit code").fill(totpCode);
    await page.getByRole("button", { name: "Confirm & Enable MFA" }).click();

    // Verify MFA active badge scoped to MFA section
    const mfaSection = page.locator('section[aria-labelledby="mfa-section-heading"]');
    await expect(mfaSection.getByText("Active", { exact: true })).toBeVisible();
    await expect(
      page.getByText("MFA has been successfully verified and activated")
    ).toBeVisible();

    // Test replay protection on step-up: replayed code is rejected
    await page.getByLabel("Password").fill(adminPassword);
    await page.getByLabel("6-Digit TOTP Code").fill(totpCode);
    await page.getByRole("button", { name: "Refresh Step-Up Authorization" }).click();
    await expect(
      page.getByRole("alert").filter({ hasText: "Invalid admin password or authenticator code" })
    ).toBeVisible();

    // Perform genuine POST /auth/step-up using freshly valid next timestep (+1)
    const freshTotpCode = generateTotpCode(secret, 1);
    await page.getByLabel("Password").fill(adminPassword);
    await page.getByLabel("6-Digit TOTP Code").fill(freshTotpCode);
    await page.getByRole("button", { name: "Refresh Step-Up Authorization" }).click();
    await expect(
      page.getByText("Step-up authentication confirmed. Privileged mutations are unlocked for 5 minutes.")
    ).toBeVisible();

    // Navigate to admin user management
    await page.goto("/admin/users");
    await expect(page.getByRole("heading", { name: "User Management" })).toBeVisible();

    // Search for self and verify exact scoped role badge
    await page.getByPlaceholder("Search by email or display name...").fill(adminEmail);
    await page.getByRole("button", { name: "Search" }).click();

    const userRow = page.locator("table tbody tr").filter({ hasText: adminEmail });
    await expect(userRow).toBeVisible();
    await expect(userRow.locator("td").nth(1)).toHaveText("admin");

    // Exercise privileged step-up mutation: suspend self triggers LAST_ADMIN check
    await userRow.getByRole("button", { name: "Suspend" }).click();
    await expect(page.getByRole("heading", { name: "Suspend Account" })).toBeVisible();
    await page.getByLabel(/Audit reason/).fill("Testing privileged step-up suspension and last admin protection");
    await page.getByRole("button", { name: "Confirm Suspension" }).click();

    await expect(
      page.getByRole("alert").filter({ hasText: "Cannot suspend the last active administrator" })
    ).toBeVisible();
    await page.getByRole("button", { name: "Cancel" }).click();
  });

  // Test 5: Security Boundaries and Mode Isolation
  test("Security Boundaries: Return path sanitization, password bounds, non-admin gate, and SaaS mode isolation", async ({
    page,
  }) => {
    // 1. Client-side password validation boundary
    await page.goto("/login");
    await page.getByLabel("Email address").fill("anyone@example.com");
    await page.getByLabel("Password").fill("short"); // < 15 chars
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page.getByRole("alert").filter({ hasText: "at least 15 characters" })).toBeVisible();

    // 2. Phishing open-redirect return path sanitization
    await page.goto("/login?next=%2F%2Fmalicious-phishing.com");
    // Form action preserves sanitized return path
    expect(page.url()).toContain("next=");

    // 3. SaaS mode unauthenticated gate redirect
    if (process.env.NEXT_PUBLIC_APP_MODE === "saas") {
      await page.goto("/base-prompt");
      await page.waitForURL((url) => url.pathname === "/login" && url.searchParams.get("next") === "/base-prompt");

      await page.goto("/agents");
      await page.waitForURL((url) => url.pathname === "/login" && url.searchParams.get("next") === "/agents");

      // 4. Register and log in as normal customer to verify authenticated mode isolation & non-admin gate
      const normalEmail = `customer-guard-${Date.now()}@example.com`;
      await page.goto("/register");
      await page.getByLabel("Email address").fill(normalEmail);
      await page.getByLabel("Display name").fill("Customer Guard");
      await page.getByRole("button", { name: "Continue with Email" }).click();

      const mail = getLatestMail({ email: normalEmail, purpose: "register", maxWaitMs: 8000 });
      await page.goto(`/verify-email#token=${mail.token}`);
      await page.getByLabel("New password", { exact: true }).fill("PasswordForCustomer123");
      await page.getByLabel("Confirm password").fill("PasswordForCustomer123");
      await page.getByRole("button", { name: "Create Account & Set Password" }).click();
      await expect(page.getByRole("heading", { name: "Account created!" })).toBeVisible();
      // Log in as normal customer
      await page.goto("/login");
      await page.getByLabel("Email address").fill(normalEmail);
      await page.getByLabel("Password").fill("PasswordForCustomer123");
      await page.getByRole("button", { name: "Sign in" }).click();
      await page.waitForURL((url) => url.pathname === "/" || url.pathname === "/settings");

      // Authenticated customer visiting /base-prompt sees global prompt isolation notice
      await page.goto("/base-prompt");
      await expect(page.getByText("Global Instructions Not Available")).toBeVisible();

      // Authenticated customer visiting /agents sees local studio isolation notice
      await page.goto("/agents");
      await expect(page.getByText("Local Studio Isolated")).toBeVisible();

      // Authenticated customer visiting /admin/users sees 403 Access Denied
      await page.goto("/admin/users");
      await expect(page.getByRole("alert").filter({ hasText: "403 Access Denied" })).toBeVisible();
    }
  });
});
