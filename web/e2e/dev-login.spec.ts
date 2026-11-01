import { test, expect } from "@playwright/test";

test("local customer shortcut submits the seeded credentials", async ({ page }) => {
  let submitted: { email?: string; password?: string } | undefined;
  await page.route("**/api/v1/auth/session", async (route) => {
    await route.fulfill({ json: { state: "anonymous", csrf_token: "csrf" } });
  });
  await page.route("**/api/v1/auth/login", async (route) => {
    submitted = route.request().postDataJSON();
    await route.fulfill({ status: 401, json: { error: { code: "INVALID_CREDENTIALS", message: "Invalid credentials", retryable: false, fields: [] } } });
  });
  await page.goto("/login");
  await page.getByRole("button", { name: "Sign in as Customer" }).click();
  await expect.poll(() => submitted?.email).toBe("customer@example.com");
  expect(submitted?.password).toBe("LocalCustomer12345!");
  await expect(page.getByRole("alert").filter({ hasText: "Invalid email or password" })).toBeVisible();
});
