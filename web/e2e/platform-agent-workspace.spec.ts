import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { expect, test, type Page, type Route } from "@playwright/test";
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
  const creationMethods = page.getByRole("group", { name: "Agent creation method" });
  const manualToggle = creationMethods.getByRole("button", { name: "Create manually" });
  const generateToggle = creationMethods.getByRole("button", { name: "Generate from prompt" });
  await expect(manualToggle).toHaveAttribute("aria-pressed", "true");
  await manualToggle.focus();
  await page.keyboard.press("Tab");
  await expect(generateToggle).toBeFocused();
  await page.keyboard.press("Space");
  await page.waitForURL((url) => url.searchParams.get("mode") === "generate");
  await expect(generateToggle).toHaveAttribute("aria-pressed", "true");
  await manualToggle.click();
  await page.waitForURL((url) => url.searchParams.get("mode") === "manual");
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
    let createdAgentId: string | null = null;
    let agentCreateCount = 0;
    let failNextDraftRead = true;
    await page.route("**/api/v1/agents", async (route) => {
      if (route.request().method() !== "POST") {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      const body = await response.json() as { id: string };
      createdAgentId = body.id;
      agentCreateCount += 1;
      await route.fulfill({
        status: response.status(),
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    });
    await page.route("**/api/v1/agents/*/draft", async (route) => {
      if (failNextDraftRead && route.request().method() === "GET") {
        failNextDraftRead = false;
        await route.abort();
        return;
      }
      await route.continue();
    });

    await page.getByRole("button", { name: "Save proposal as draft" }).tap();
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.getByRole("link", { name: "Open the created draft" })).toBeVisible();
    expect(createdAgentId).not.toBeNull();

    const editedDraftPage = await page.context().newPage();
    await editedDraftPage.goto(`/agents/${createdAgentId}`);
    await editedDraftPage.getByRole("button", { name: "Add step" }).click();
    await editedDraftPage.getByRole("button", { name: "Flow JSON" }).click();
    const editedFlow = JSON.parse(await editedDraftPage.getByLabel("Flow document").inputValue()) as { steps: unknown[] };
    await editedDraftPage.getByRole("button", { name: "Save draft" }).click();
    await expect(editedDraftPage.getByText("Saved r2", { exact: true })).toBeVisible();

    await page.reload();
    await expect(page.getByRole("heading", { name: "COMPLETED", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Save proposal as draft" }).tap();
    await expect(page.getByRole("alert")).toContainText("The created draft changed from r1 to r2");
    expect(agentCreateCount).toBe(1);

    await editedDraftPage.reload();
    await editedDraftPage.getByRole("button", { name: "Flow JSON" }).click();
    const recoveredFlow = JSON.parse(await editedDraftPage.getByLabel("Flow document").inputValue()) as { steps: unknown[] };
    expect(recoveredFlow.steps).toEqual(editedFlow.steps);
    await expect(editedDraftPage.getByText("Not published", { exact: true })).toBeVisible();
  });
});

test("Chat Test uses a saved executable snapshot and reset keeps its hash but starts a new test ID", async ({ page }) => {
  test.setTimeout(180_000);
  await registerAndSignIn(page);
  const agentId = await createBlankAgent(page, `Chat test ${Date.now()}`);

  await page.getByRole("button", { name: "Flow JSON" }).click();
  await page.getByLabel("Flow document").fill(JSON.stringify(loanFixture.flow, null, 2));
  await page.getByRole("button", { name: "Apply flow JSON" }).click();
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText(/Saved r2/)).toBeVisible();
  await expect(page.getByText("This draft has no executable snapshot yet.")).toHaveCount(0);

  await page.getByLabel("Message the agent").fill("I am salaried and earn 100000 rupees per month.");
  await page.getByRole("button", { name: "Send message" }).click();
  const transcript = page.getByRole("list", { name: "Chat transcript" });
  await expect(transcript.getByText("You · turn 1")).toBeVisible({ timeout: 120_000 });
  await expect(transcript.getByText("Agent · turn 1")).toBeVisible({ timeout: 120_000 });
  await expect(transcript.locator("li")).toHaveCount(2);

  await page.reload();
  const recoveredTranscript = page.getByRole("list", { name: "Chat transcript" });
  await expect(recoveredTranscript.getByText("You · turn 1")).toBeVisible();
  await expect(recoveredTranscript.getByText("Agent · turn 1")).toBeVisible();

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

test("unsaved persona changes block validation and publishing", async ({ page }) => {
  await registerAndSignIn(page);
  await createBlankAgent(page, `Identity gate ${Date.now()}`);
  await page.getByRole("button", { name: "Flow JSON" }).click();
  await page.getByLabel("Flow document").fill(JSON.stringify(loanFixture.flow, null, 2));
  await page.getByRole("button", { name: "Apply flow JSON" }).click();
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText("Saved r2", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Validate current draft" }).click();
  await expect(page.getByText("This draft is eligible for publishing.")).toBeVisible();
  const warnings = page.locator("fieldset input[type=checkbox]");
  for (let index = 0; index < await warnings.count(); index += 1) {
    await warnings.nth(index).check();
  }
  await page.getByLabel("Version notes").fill("Publish identity gate regression check");
  const publishButton = page.getByRole("button", { name: "Publish version" });
  await expect(publishButton).toBeEnabled();

  await page.getByLabel("Persona instructions").fill("Use concise answers and do not guess.");
  await expect(page.getByRole("status").filter({ hasText: "Unsaved agent details" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Validate current draft" })).toBeDisabled();
  await page.getByRole("button", { name: "Save agent details" }).click();
  await expect(page.getByRole("button", { name: "Validate current draft" })).toBeEnabled();
  await expect(publishButton).toBeDisabled();
  await page.getByRole("button", { name: "Validate current draft" }).click();
  await expect(page.getByText("This draft is eligible for publishing.")).toBeVisible();
  for (let index = 0; index < await warnings.count(); index += 1) {
    await warnings.nth(index).check();
  }
  await expect(publishButton).toBeEnabled();

});

test("history retry preserves edits and warns before leaving the workspace", async ({ page }) => {
  await registerAndSignIn(page);
  const historyRoute = "**/api/v1/agents/*/versions*";
  const failHistory = async (route: Route) => route.abort();
  await page.route(historyRoute, failHistory);
  const agentId = await createBlankAgent(page, `History recovery ${Date.now()}`);
  await expect(page.getByRole("alert").filter({ hasText: "Could not load version history" })).toBeVisible();

  const persona = "Keep answers brief and based on confirmed facts.";
  await page.getByLabel("Persona instructions").fill(persona);
  await expect(page.getByRole("status").filter({ hasText: "Unsaved agent details" })).toBeVisible();
  await page.unroute(historyRoute, failHistory);
  await page.getByRole("button", { name: "Retry version history" }).click();
  await expect(page.getByText("No published versions yet.")).toBeVisible();
  await expect(page.getByLabel("Persona instructions")).toHaveValue(persona);
  const flowControls = page.getByRole("group", { name: "Flow editor view" });
  const flowStepsButton = flowControls.getByRole("button", { name: "Steps" });
  const flowJsonButton = flowControls.getByRole("button", { name: "Flow JSON" });
  await expect(flowControls).toHaveAttribute("role", "group");
  await flowStepsButton.focus();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await expect(flowJsonButton).toBeFocused();
  await page.keyboard.press("Space");
  await expect(flowJsonButton).toHaveAttribute("aria-pressed", "true");

  await page.getByRole("button", { name: "Flow JSON" }).click();
  const rawFlow = JSON.stringify(loanFixture.flow, null, 2);
  const flowInput = page.getByLabel("Flow document");
  await flowInput.fill(rawFlow);
  await expect(page.getByRole("status").filter({ hasText: "Unapplied Flow JSON" })).toBeVisible();
  await page.getByRole("button", { name: "Steps" }).click();
  await page.getByRole("button", { name: "Add step" }).click();
  await expect(flowInput).toHaveValue(rawFlow);
  await expect(page.getByText(/Your JSON text is preserved/)).toBeVisible();

  const navigationDialog = page.waitForEvent("dialog");
  await page.getByRole("link", { name: "Back to agents" }).click();
  const dialog = await navigationDialog;
  expect(dialog.type()).toBe("confirm");
  await dialog.dismiss();
  expect(page.url()).toContain(`/agents/${agentId}`);
  await expect(page.getByLabel("Persona instructions")).toHaveValue(persona);
  await expect(flowInput).toHaveValue(rawFlow);
});

test("duplicate retry reuses its idempotency key after response loss and reload", async ({ page }) => {
  await registerAndSignIn(page);
  const agentName = `Duplicate recovery ${Date.now()}`;
  await page.goto("/agents");
  await openCreateMenuByKeyboard(page);
  await page.waitForURL((url) => url.pathname === "/agents/new" && url.searchParams.get("mode") === "manual");
  await page.getByLabel("Agent name").fill(agentName);
  await page.getByRole("button", { name: "Create draft and configure" }).click();
  await page.waitForURL(/\/agents\/[0-9a-f-]+$/);
  await page.goto("/agents");

  let firstIdempotencyKey = "";
  const duplicateRoute = `**/api/v1/agents/*/duplicate`;
  const loseDuplicateResponse = async (route: Route) => {
    firstIdempotencyKey = route.request().headers()["idempotency-key"] ?? "";
    await route.fetch();
    await route.abort();
  };
  await page.route(duplicateRoute, loseDuplicateResponse);
  await page.getByRole("button", { name: `Duplicate ${agentName}` }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(firstIdempotencyKey).not.toBe("");
  await page.unroute(duplicateRoute, loseDuplicateResponse);
  await page.reload();

  const retryResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" &&
    new URL(response.url()).pathname.endsWith("/duplicate")
  );
  await page.getByRole("button", { name: `Duplicate ${agentName}` }).click();
  const replay = await retryResponse;
  expect(replay.request().headers()["idempotency-key"]).toBe(firstIdempotencyKey);
  await expect(page.getByRole("heading", { name: `Copy of ${agentName}`, exact: true })).toHaveCount(1);
});

test("Chat Test reloads a pending message and retries the exact persisted turn", async ({ page }) => {
  await registerAndSignIn(page);
  const agentId = await createBlankAgent(page, `Pending chat ${Date.now()}`);
  await page.getByRole("button", { name: "Flow JSON" }).click();
  await page.getByLabel("Flow document").fill(JSON.stringify(loanFixture.flow, null, 2));
  await page.getByRole("button", { name: "Apply flow JSON" }).click();
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText("Saved r2", { exact: true })).toBeVisible();

  let pinnedRequest: Record<string, unknown> | null = null;
  const sends: Record<string, unknown>[] = [];
  let testId = "";
  await page.route(`**/api/v1/agents/${agentId}/test/chat`, async (route) => {
    if (route.request().method() === "POST") {
      const body = JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>;
      sends.push(body);
      testId = typeof body.test_id === "string" ? body.test_id : "";
      pinnedRequest ??= body;
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "REVISION_CONFLICT",
            message: "The previous Chat Test turn is still processing",
            request_id: "pending-turn",
            retryable: true,
            fields: [],
          },
        }),
      });
      return;
    }
    const request = pinnedRequest;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        test_id: testId,
        snapshot_hash: request?.snapshot_hash,
        turn_sequence: 0,
        pending_turn_sequence: 1,
        messages: [{
          id: "pending-user-message",
          turn_sequence: 1,
          role: "user",
          content: "Keep this exact message while the same turn is pending.",
          created_at: new Date().toISOString(),
        }],
        flow_state: {},
        trace: [],
        outcome: "in_progress",
      }),
    });
  });

  const text = "Keep this exact message while the same turn is pending.";
  await page.getByLabel("Message the agent").fill(text);
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByRole("button", { name: "Retry pending turn" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Chat transcript" }).getByText("You · turn 1 · pending")).toBeVisible();
  await expect(page.getByLabel("Message the agent")).toHaveValue(text);

  await page.reload();
  await expect(page.getByRole("list", { name: "Chat transcript" }).getByText("You · turn 1 · pending")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry pending turn" })).toBeVisible();
  await expect(page.getByLabel("Message the agent")).toHaveValue(text);
  await page.getByRole("button", { name: "Retry pending turn" }).click();
  await expect(page.getByRole("button", { name: "Retry pending turn" })).toBeVisible();
  expect(sends).toHaveLength(2);
  expect(sends[1]).toEqual(sends[0]);
  expect(sends[1]).toMatchObject({
    test_id: testId,
    expected_turn_sequence: 1,
    text,
  });
});
