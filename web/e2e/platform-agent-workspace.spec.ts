import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { getLatestMail, WORKTREE_ROOT } from "./fixtures/auth";

const PASSWORD = "PlatformWorkspacePass12345";
const loanFixture = JSON.parse(
  readFileSync(path.join(WORKTREE_ROOT, "docs/first-release/flow-fixtures/valid-loan-en-hi.json"), "utf8")
) as { flow: Record<string, unknown> };

async function registerAndSignIn(page: Page): Promise<void> {
  const email = `platform-task4-${randomUUID()}@example.com`;
  await page.goto("/register");
  await page.getByLabel("Email address").fill(email);
  await page.getByLabel("Display name").fill("Platform Workspace Customer");
  await page.getByRole("button", { name: "Continue with Email" }).click();
  await expect(page.getByRole("heading", { name: "Check your inbox" })).toBeVisible();

  const mail = getLatestMail({ email, purpose: "register", maxWaitMs: 8000 });
  expect(mail.token).toBeTruthy();
  await page.goto(`/verify-email#token=${mail.token}`);
  await page.getByLabel("New password").fill(PASSWORD);
  await page.getByLabel("Confirm password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create Account & Set Password" }).click();
  await expect(page.getByRole("heading", { name: "Account created!" })).toBeVisible();

  await page.goto("/login");
  await page.getByLabel("Email address").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((url) => url.pathname === "/" || url.pathname === "/settings");
}

async function openCreateMenuByKeyboard(page: Page): Promise<void> {
  const summary = page.locator("summary").filter({ hasText: "Create agent" }).first();
  await summary.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
}

async function createBlankAgent(page: Page, name: string): Promise<string> {
  await page.goto("/agents");
  await openCreateMenuByKeyboard(page);
  await page.waitForURL((url) => url.pathname === "/agents/new" && url.searchParams.get("mode") === "manual");
  await page.getByLabel("Agent name").fill(name);
  await page.getByRole("button", { name: "Create draft and configure" }).click();
  await page.waitForURL(/\/agents\/[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
  const parts = page.url().split("/");
  return parts[parts.length - 1]!;
}

test("keyboard creation keeps incomplete drafts and surfaces a real stale-revision conflict", async ({ page }) => {
  await registerAndSignIn(page);
  await page.goto("/agents");
  await expect(page.getByRole("heading", { name: "No agents yet" })).toBeVisible();

  const agentId = await createBlankAgent(page, `Keyboard draft ${Date.now()}`);
  const stalePage = await page.context().newPage();
  await stalePage.goto(`/agents/${agentId}`);
  await expect(stalePage.getByText("Draft r1", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Add step" }).click();
  await expect(page.getByRole("heading", { name: "Step 1: New step" })).toBeVisible();
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText("Saved r2", { exact: true })).toBeVisible();

  await stalePage.getByLabel("Entry step ID").fill("unpublished_entry");
  await expect(stalePage.getByRole("alert").filter({ hasText: "This draft changed on the server" })).toBeVisible();
  await expect(stalePage.getByLabel("Entry step ID")).toHaveValue("unpublished_entry");
  await expect(stalePage.getByText("Not published", { exact: true })).toBeVisible();
  await stalePage.close();
});

test.describe("touch creation", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test("creates and reviews a real bounded generation proposal without publishing", async ({ page }) => {
    test.setTimeout(150_000);
    await registerAndSignIn(page);
    await page.goto("/agents");
    await expect(page.getByRole("heading", { name: "No agents yet" })).toBeVisible();

    await page.locator("summary").filter({ hasText: "Create agent" }).first().tap();
    await page.getByRole("link", { name: "Generate from prompt" }).tap();
    await page.getByLabel("What should the agent do?").fill(
      "Help customers check whether a returned item may qualify for a refund. Ask for the order number and return date. List unknown policy details for confirmation; do not invent eligibility rules or fees."
    );
    const modelSelect = page.getByLabel("Generation model");
    await expect(modelSelect).toBeEnabled();
    await modelSelect.selectOption({ index: 1 });
    await page.getByLabel("Token budget").fill("512");
    await page.getByRole("button", { name: "Generate proposal" }).tap();

    await expect(page.getByRole("heading", { name: "COMPLETED", exact: true })).toBeVisible({ timeout: 120_000 });
    await expect(page.getByRole("button", { name: "Save proposal as draft" })).toBeVisible();
    await page.getByRole("button", { name: "Save proposal as draft" }).tap();
    await page.waitForURL(/\/agents\/[0-9a-f-]+$/);
    await expect(page.getByText("Not published", { exact: true })).toBeVisible();
  });
});

test("Chat Test uses a saved executable snapshot and reset keeps its hash but starts a new test ID", async ({ page }) => {
  test.setTimeout(180_000);
  await registerAndSignIn(page);
  const agentId = await createBlankAgent(page, `Chat test ${Date.now()}`);

  await page.getByRole("tab", { name: "Flow JSON" }).click();
  await page.getByLabel("Flow document").fill(JSON.stringify(loanFixture.flow, null, 2));
  await page.getByRole("button", { name: "Apply flow JSON" }).click();
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText(/Saved r2/)).toBeVisible();
  await expect(page.getByText("This draft has no executable snapshot yet.")).toHaveCount(0);

  await page.getByLabel("Message the agent").fill("I am salaried and earn 100000 rupees per month.");
  await page.getByRole("button", { name: "Send message" }).click();
  const transcript = page.getByRole("list", { name: "Committed chat transcript" });
  await expect(transcript.getByText("You · turn 1")).toBeVisible({ timeout: 120_000 });
  await expect(transcript.getByText("Agent · turn 1")).toBeVisible({ timeout: 120_000 });
  await expect(transcript.locator("li")).toHaveCount(2);

  const pinnedHeading = page.getByText(new RegExp(`Pinned to Draft r\\d+`));
  const pinnedHash = await pinnedHeading.locator("..").locator("p").nth(1).innerText();
  const firstTestId = await page.getByText("Test ID:").locator("span").innerText();
  await page.getByRole("button", { name: "Reset on pinned snapshot" }).click();
  const resetTestId = await page.getByText("Test ID:").locator("span").innerText();
  const resetHash = await page.getByText(new RegExp(`Pinned to Draft r\\d+`)).locator("..").locator("p").nth(1).innerText();
  expect(resetTestId).not.toBe(firstTestId);
  expect(resetHash).toBe(pinnedHash);

  const identityPage = await page.context().newPage();
  await identityPage.goto(`/agents/${agentId}`);
  await expect(identityPage.getByRole("heading", { level: 1, name: new RegExp(`Chat test`) })).toBeVisible();
  const identityUpdate = identityPage.waitForResponse((response) =>
    response.request().method() === "PATCH" &&
    new URL(response.url()).pathname.endsWith(`/agents/${agentId}`)
  );
  const draftRefresh = identityPage.waitForResponse((response) =>
    response.request().method() === "GET" &&
    new URL(response.url()).pathname.endsWith(`/agents/${agentId}/draft`)
  );
  await identityPage.getByLabel("Persona instructions").fill("Give concise answers and ask only for confirmed facts.");
  await identityPage.getByRole("button", { name: "Save agent details" }).click();
  const [identityResponse, refreshedDraftResponse] = await Promise.all([identityUpdate, draftRefresh]);
  expect(identityResponse.ok()).toBe(true);
  expect(refreshedDraftResponse.ok()).toBe(true);
  await identityPage.close();

  const uncommittedMessage = "Continue from the latest confirmed details.";
  await page.getByLabel("Message the agent").fill(uncommittedMessage);
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByRole("alert")).toContainText("The pinned snapshot was rejected");
  await expect(page.getByRole("button", { name: "Start on selected snapshot" })).toBeVisible();
  await page.getByRole("button", { name: "Start on selected snapshot" }).click();
  const currentTestId = await page.getByText("Test ID:").locator("span").innerText();
  const currentHash = await page.getByText(new RegExp(`Pinned to Draft r\\d+`)).locator("..").locator("p").nth(1).innerText();
  expect(currentTestId).not.toBe(resetTestId);
  expect(currentHash).not.toBe(resetHash);
  await expect(page.getByLabel("Message the agent")).toHaveValue(uncommittedMessage);
  expect(page.url()).toContain(`/agents/${agentId}`);
});
