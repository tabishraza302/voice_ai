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
  await stalePage.getByRole("button", { name: "Save draft" }).click();
  await expect(
    stalePage.getByRole("alert").filter({ hasText: "This draft changed on the server" })
  ).toBeVisible();
  await expect(stalePage.getByLabel("Entry step ID")).toHaveValue("unpublished_entry");
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
    await expect(
      page.getByRole("alert").filter({ has: page.getByRole("link", { name: "Open the created draft" }) })
    ).toBeVisible();
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
    await expect(
      page.getByRole("alert").filter({ hasText: "The created draft changed from r1 to r2" })
    ).toContainText("The created draft changed from r1 to r2");
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
  await page.getByRole("button", { name: "Chat Test" }).click();
  await expect(page.getByText("This draft has no executable snapshot yet.")).toHaveCount(0);

  await page.getByLabel("Message the agent").fill("I am salaried and earn 100000 rupees per month.");
  await page.getByRole("button", { name: "Send message" }).click();
  const transcript = page.getByRole("list", { name: "Chat transcript" });
  await expect(transcript.getByText("You · turn 1")).toBeVisible({ timeout: 120_000 });
  await expect(transcript.getByText("Agent · turn 1")).toBeVisible({ timeout: 120_000 });
  await expect(transcript.locator("li")).toHaveCount(2);

  await page.reload();
  await page.getByRole("button", { name: "Chat Test" }).click();
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
  await identityPage.getByRole("button", { name: "Save draft" }).click();
  const [identityResponse, refreshedDraftResponse] = await Promise.all([identityUpdate, draftRefresh]);
  expect(identityResponse.ok()).toBe(true);
  expect(refreshedDraftResponse.ok()).toBe(true);
  await identityPage.close();

  const uncommittedMessage = "Continue from the latest confirmed details.";
  await page.getByLabel("Message the agent").fill(uncommittedMessage);
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "The pinned snapshot was rejected" })
  ).toContainText("The pinned snapshot was rejected");
  await expect(page.getByRole("button", { name: "Start on selected snapshot" })).toBeVisible();
  await page.getByRole("button", { name: "Start on selected snapshot" }).click();
  const currentTestId = await page.getByText("Test ID:").locator("span").innerText();
  const currentHash = await page.getByText(new RegExp(`Pinned to Draft r\\d+`)).locator("..").locator("p").nth(1).innerText();
  expect(currentTestId).not.toBe(resetTestId);
  expect(currentHash).not.toBe(resetHash);
  await expect(page.getByLabel("Message the agent")).toHaveValue(uncommittedMessage);
  expect(page.url()).toContain(`/agents/${agentId}`);
});

test("unapplied Flow JSON and unsaved persona changes block validation and publishing", async ({ page }) => {
  await registerAndSignIn(page);
  await createBlankAgent(page, `Identity gate ${Date.now()}`);
  await page.getByRole("button", { name: "Flow JSON" }).click();
  await page.getByLabel("Flow document").fill(JSON.stringify(loanFixture.flow, null, 2));
  await page.getByRole("button", { name: "Apply flow JSON" }).click();
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText("Saved r2", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "Releases & History" }).click();
  const validateButton = page.getByRole("button", { name: "Validate current draft" });
  const publishButton = page.getByRole("button", { name: "Publish version" });
  await expect(validateButton).toBeEnabled();
  await expect(publishButton).toBeDisabled();

  await page.getByRole("tab", { name: "Builder" }).click();
  await page.getByLabel("Persona instructions").fill("Use concise answers and do not guess.");
  await expect(page.getByRole("status").filter({ hasText: "Unsaved agent details" })).toBeVisible();
  await page.getByRole("tab", { name: "Releases & History" }).click();
  await expect(validateButton).toBeDisabled();
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(validateButton).toBeEnabled();
  await expect(publishButton).toBeDisabled();

  await page.getByRole("tab", { name: "Builder" }).click();
  await page.getByRole("button", { name: "Flow JSON", exact: true }).click();
  const flowInput = page.getByLabel("Flow document");
  const unappliedFlow = JSON.stringify(loanFixture.flow, null, 2)
    .replace("Hi! I can help discuss a loan.", "Hello! I can help discuss a loan.");
  await flowInput.fill(unappliedFlow);
  await expect(page.getByRole("status").filter({ hasText: "Unapplied Flow JSON" })).toBeVisible();
  await page.getByRole("tab", { name: "Releases & History" }).click();
  await expect(validateButton).toBeDisabled();
  await expect(publishButton).toBeDisabled();
  await page.getByRole("tab", { name: "Builder" }).click();
  await expect(flowInput).toHaveValue(unappliedFlow);
  await expect(page.getByRole("status").filter({ hasText: "Unapplied Flow JSON" })).toBeVisible();
  await page.getByRole("tab", { name: "Releases & History" }).click();
  await expect(validateButton).toBeDisabled();
  await expect(publishButton).toBeDisabled();

});

test("unapplied Flow JSON and step dropdowns survive workspace tab switches", async ({ page }) => {
  await registerAndSignIn(page);
  await createBlankAgent(page, `Flow state ${Date.now()}`);
  await page.getByRole("button", { name: "Add step" }).click();

  const stepType = page.getByLabel("Type");
  await stepType.selectOption("collect");
  await page.getByRole("button", { name: "Flow JSON" }).click();
  const flowInput = page.getByLabel("Flow document");
  const flowDraft = JSON.parse(await flowInput.inputValue()) as Record<string, unknown>;
  flowDraft.workspace_test_marker = "preserve while switching tabs";
  const unappliedFlow = JSON.stringify(flowDraft, null, 2);
  await flowInput.fill(unappliedFlow);
  await expect(page.getByRole("status").filter({ hasText: "Unapplied Flow JSON" })).toBeVisible();

  await page.getByRole("tab", { name: "Releases & History" }).click();
  await expect(page.getByRole("button", { name: "Validate current draft" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Publish version" })).toBeDisabled();
  await page.getByRole("tab", { name: "Builder" }).click();
  await expect(flowInput).toHaveValue(unappliedFlow);
  await expect(page.getByRole("status").filter({ hasText: "Unapplied Flow JSON" })).toBeVisible();
  await page.getByRole("button", { name: "Steps" }).click();
  await expect(stepType).toHaveValue("collect");
});

test("speech dropdowns show registered models and block unqualified choices", async ({ page }) => {
  await registerAndSignIn(page);
  await createBlankAgent(page, `Speech catalog ${Date.now()}`);
  const response = await page.request.get("/api/v1/catalog");
  expect(response.ok()).toBe(true);
  const catalog = await response.json() as {
    stt_providers: { id: string; name: string; models: { id: string; name: string; selectable: boolean }[] }[];
    tts_providers: { id: string; name: string; models: { id: string; name: string; selectable: boolean; voices: { name: string; language: string; selectable: boolean }[] }[] }[];
  };
  const stt = catalog.stt_providers.find((provider) => provider.models.length > 0);
  const tts = catalog.tts_providers.find((provider) => provider.models.some((model) => model.voices.length > 0));
  expect(stt).toBeTruthy();
  expect(tts).toBeTruthy();

  await page.getByRole("button", { name: /Configure speech-to-text/ }).click();
  const sttPanel = page.getByRole("region", { name: "Speech-to-text provider and model settings" });
  const sttOption = sttPanel.getByRole("region", { name: stt!.name }).getByRole("button", { name: new RegExp(stt!.models[0]!.name) });
  await expect(sttOption).toHaveCount(1);
  if (!stt!.models[0]!.selectable) await expect(sttOption).toBeDisabled();
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: /Configure text-to-speech/ }).click();
  const ttsPanel = page.getByRole("region", { name: "Text-to-speech provider, model, and voice settings" });
  const model = tts!.models.find((item) => item.voices.length > 0)!;
  const voice = model.voices[0]!;
  const escapedVoice = voice.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const ttsOption = ttsPanel.getByRole("region", { name: tts!.name }).getByRole("button", { name: new RegExp(escapedVoice) });
  await expect(ttsOption).toHaveCount(1);
  if (!model.selectable || !voice.selectable) await expect(ttsOption).toBeDisabled();
});

test("history retry preserves edits and warns before leaving the workspace", async ({ page }) => {
  await registerAndSignIn(page);
  const historyRoute = "**/api/v1/agents/*/versions*";
  const failHistory = async (route: Route) => route.abort();
  await page.route(historyRoute, failHistory);
  const agentId = await createBlankAgent(page, `History recovery ${Date.now()}`);
  await page.getByRole("tab", { name: "Releases & History" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Could not load version history" })).toBeVisible();

  await page.getByRole("tab", { name: "Builder" }).click();
  const persona = "Keep answers brief and based on confirmed facts.";
  await page.getByLabel("Persona instructions").fill(persona);
  await expect(page.getByRole("status").filter({ hasText: "Unsaved agent details" })).toBeVisible();
  await page.unroute(historyRoute, failHistory);
  await page.getByRole("tab", { name: "Releases & History" }).click();
  await page.getByRole("button", { name: "Retry version history" }).click();
  await expect(page.getByText("No published versions yet.")).toBeVisible();
  await page.getByRole("tab", { name: "Builder" }).click();
  await expect(page.getByLabel("Persona instructions")).toHaveValue(persona);
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByRole("status").filter({ hasText: /Saved r\d+/ })).toBeVisible();
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

  await page.getByRole("button", { name: "Flow JSON", exact: true }).click();
  const rawFlow = JSON.stringify(loanFixture.flow, null, 2);
  const flowInput = page.getByLabel("Flow document");
  await flowInput.fill(rawFlow);
  await expect(page.getByRole("status").filter({ hasText: "Unapplied Flow JSON" })).toBeVisible();
  await page.getByRole("button", { name: "Steps" }).click();
  await page.getByRole("button", { name: "Add step" }).click();
  await page.getByRole("button", { name: "Flow JSON", exact: true }).click();
  await expect(flowInput).toHaveValue(rawFlow);
  await expect(page.getByText(/Your JSON text is preserved/)).toBeVisible();

  let navigationDialogType: string | null = null;
  page.once("dialog", async (dialog) => {
    navigationDialogType = dialog.type();
    await dialog.dismiss();
  });
  await page.getByRole("link", { name: "Back to agents" }).click();
  expect(navigationDialogType).toBe("confirm");
  expect(page.url()).toContain(`/agents/${agentId}`);
  await page.getByRole("tab", { name: "Builder" }).click();
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
  await expect(
    page.getByRole("alert").filter({ has: page.getByRole("button", { name: "Dismiss" }) })
  ).toBeVisible();
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
  await page.route(`**/api/v1/agents/${agentId}/test/chat**`, async (route) => {
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
        turn_sequence: 1,
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
  await page.getByRole("button", { name: "Chat Test" }).click();
  const text = "Keep this exact message while the same turn is pending.";
  await page.getByLabel("Message the agent").fill(text);
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByRole("button", { name: "Retry pending turn" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Chat transcript" }).getByText("You · turn 1 · pending")).toBeVisible();
  await expect(page.getByLabel("Message the agent")).toHaveValue(text);

  await page.reload();
  await page.getByRole("button", { name: "Chat Test" }).click();
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

test("Flow QA lists frozen cases, shows results, keeps suggestions draft-only, and cancels jobs", async ({ page }) => {
  test.setTimeout(180_000);
  await registerAndSignIn(page);
  const scenario = {
    scenario_id: "qa_scenario_branch_1",
    case_key: "branch:income:0:review",
    kind: "branch",
    expected_edge: {
      from_step_id: "income",
      transition_index: 0,
      to_step_id: "review",
      when: { operator: "eq", field: "employment", value: "salaried" },
    },
    target_step_id: "income",
    target_fields: ["employment"],
    task: "Reach the review step with the frozen answer.",
  };
  const suggestions = [
    {
      suggestion_id: "qa_sug_apply",
      status: "DRAFT",
      draft_only: true,
      title: "Review step income",
      summary: "The flow should clarify the selected employment state.",
      target_step_id: "income",
      target_fields: ["employment"],
      evidence: { scenario_id: scenario.scenario_id, assertion: "The flow did not clarify." },
      proposed_patch: { path: "/steps/income/instruction", append_text: "Confirm the employment state." },
    },
    {
      suggestion_id: "qa_sug_dismiss",
      status: "DRAFT",
      draft_only: true,
      title: "Review step follow-up",
      summary: "The flow should re-check the amount.",
      target_step_id: "income",
      target_fields: ["monthly_income"],
      evidence: { scenario_id: scenario.scenario_id, assertion: "The amount was not confirmed." },
      proposed_patch: { path: "/steps/income/instruction", append_text: "Confirm the amount." },
    },
  ];
  const requests: { method: string; path: string; search: string; body: Record<string, unknown> | null }[] = [];
  const jobs = new Map<string, {
    request: Record<string, unknown>;
    status: "COMPLETED" | "RUNNING" | "CANCELLED";
    suggestions: Record<string, unknown>[];
  }>();
  const createBodies: Record<string, unknown>[] = [];
  const publishCalls: string[] = [];
  let nextJob = 0;

  await page.route("**/api/v1/generation-models", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [{
          id: "qa-utterance",
          name: "QA Utterance",
          description: "Qualified test model",
          supports_reasoning: false,
        }],
      }),
    });
  });

  await page.route("**/api/v1/qa/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const body = method === "POST"
      ? JSON.parse(request.postData() ?? "{}") as Record<string, unknown>
      : null;
    requests.push({ method, path: url.pathname, search: url.search, body });
    const respond = (value: unknown, status = 200) => route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(value),
    });

    if (url.pathname === "/api/v1/qa/scenarios" && method === "GET") {
      return respond({
        scenarios: [scenario],
        scenario_count: 1,
        omitted_scenario_count: 0,
        flow_hash: "a".repeat(64),
      });
    }
    if (url.pathname === "/api/v1/qa/jobs" && method === "POST") {
      nextJob += 1;
      const jobId = `qa-job-${nextJob}`;
      createBodies.push(body ?? {});
      jobs.set(jobId, {
        request: body ?? {},
        status: nextJob === 1 ? "COMPLETED" : "RUNNING",
        suggestions: nextJob === 1 ? suggestions.map((item) => ({ ...item })) : [],
      });
      return respond({ job_id: jobId, status: "QUEUED" }, 202);
    }
    const jobMatch = url.pathname.match(/\/api\/v1\/qa\/jobs\/([^/]+)$/);
    if (jobMatch && method === "GET") {
      const jobId = jobMatch[1]!;
      const stored = jobs.get(jobId);
      if (!stored) return respond({ error: "Missing test job" }, 404);
      const jobStatus = stored.status;
      return respond({
        job_id: jobId,
        agent_id: stored.request.agent_id,
        snapshot_kind: "draft",
        snapshot_hash: stored.request.snapshot_hash,
        status: jobStatus,
        config_hash: "b".repeat(64),
        rubric_version: "flow-qa-rubric/1",
        models: { runtime: "qwen3.5:4b", utterance: "qa-utterance", judge: null },
        scenarios: [scenario],
        results: jobStatus === "COMPLETED" ? [{
          scenario_id: scenario.scenario_id,
          kind: scenario.kind,
          status: "FAIL",
          assertion: "The flow did not clarify the selected state.",
          expected_edge: scenario.expected_edge,
          observed_edges: [],
          turn_count: 1,
          token_usage: { prompt_tokens: 40, completion_tokens: 12, total_tokens: 52 },
          artifact_id: "qa_artifact_1",
          failure_code: null,
        }] : [],
        suggestions: stored.suggestions,
        coverage: {
          scenario_count: 1,
          covered_edge_count: 1,
          scenario_status_counts: { PASS: 0, FAIL: 1, INCONCLUSIVE: 0, ERROR: 0 },
        },
        budget: { scenario_count: 1, max_turns: 12, max_tokens: 4096, max_runtime_seconds: 120 },
        usage: { prompt_tokens: 40, completion_tokens: 12, total_tokens: 52 },
        failure_code: null,
        runtime_seconds: 1.2,
        artifacts: [],
        created_at: new Date().toISOString(),
        started_at: new Date().toISOString(),
        completed_at: jobStatus === "COMPLETED" || jobStatus === "CANCELLED" ? new Date().toISOString() : null,
      });
    }
    const cancelMatch = url.pathname.match(/\/api\/v1\/qa\/jobs\/([^/]+)\/cancel$/);
    if (cancelMatch && method === "POST") {
      const stored = jobs.get(cancelMatch[1]!);
      if (!stored) return respond({ error: "Missing test job" }, 404);
      stored.status = "CANCELLED";
      return respond({ job_id: cancelMatch[1], status: "CANCELLED" });
    }
    const applyMatch = url.pathname.match(/\/api\/v1\/qa\/jobs\/([^/]+)\/suggestions\/([^/]+)\/apply$/);
    if (applyMatch && method === "POST") {
      const stored = jobs.get(applyMatch[1]!);
      const suggestion = stored?.suggestions.find((item) => item.suggestion_id === applyMatch[2]);
      if (!stored || !suggestion) return respond({ error: "Missing suggestion" }, 404);
      suggestion.status = "APPLIED_TO_DRAFT";
      return respond({
        suggestion_id: applyMatch[2],
        draft_revision: 3,
        authoring_hash: "c".repeat(64),
        applied: true,
      });
    }
    const dismissMatch = url.pathname.match(/\/api\/v1\/qa\/jobs\/([^/]+)\/suggestions\/([^/]+)\/dismiss$/);
    if (dismissMatch && method === "POST") {
      const stored = jobs.get(dismissMatch[1]!);
      const suggestion = stored?.suggestions.find((item) => item.suggestion_id === dismissMatch[2]);
      if (!stored || !suggestion) return respond({ error: "Missing suggestion" }, 404);
      suggestion.status = "DISMISSED";
      return respond({ suggestion_id: dismissMatch[2], status: "DISMISSED" });
    }
    return respond({ error: "Unmocked QA route" }, 404);
  });

  const agentId = await createBlankAgent(page, `Flow QA ${Date.now()}`);
  await page.route(`**/api/v1/agents/${agentId}/publish`, async (route) => {
    publishCalls.push(route.request().url());
    await route.fulfill({ status: 409, contentType: "application/json", body: "{}" });
  });
  await page.getByRole("button", { name: "Flow JSON" }).click();
  await page.getByLabel("Flow document").fill(JSON.stringify(loanFixture.flow, null, 2));
  await page.getByRole("button", { name: "Apply flow JSON" }).click();
  const saveResponse = page.waitForResponse((response) =>
    response.request().method() === "PATCH" &&
    new URL(response.url()).pathname.endsWith(`/agents/${agentId}/draft`)
  );
  await page.getByRole("button", { name: "Save draft" }).click();
  const savedDraftResponse = await saveResponse;
  expect(savedDraftResponse.ok()).toBe(true);
  const savedDraft = await savedDraftResponse.json() as { snapshot_hash: string; draft_revision: number };
  expect(savedDraft.snapshot_hash).toBeTruthy();
  await page.getByRole("button", { name: "Flow QA" }).click();
  await expect(page.getByRole("heading", { name: "Flow QA" })).toBeVisible();
  await page.getByRole("button", { name: "Load scenarios" }).click();
  await expect(page.getByLabel(`Include ${scenario.case_key}`)).toBeChecked();
  const previewRequest = requests.find((item) => item.path === "/api/v1/qa/scenarios");
  expect(previewRequest?.method).toBe("GET");
  const previewQuery = new URLSearchParams(previewRequest?.search ?? "");
  expect(previewQuery.get("agent_id")).toBe(agentId);
  expect(previewQuery.get("snapshot_hash")).toBe(savedDraft.snapshot_hash);
  expect(previewQuery.get("scenario_count")).toBe("64");

  await page.getByRole("button", { name: "Run selected scenarios" }).click();
  await expect(page.getByRole("list", { name: "QA results" }).getByText("The flow did not clarify the selected state.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Coverage summary" })).toBeVisible();
  await expect(page.getByText("1 unique transitions observed.")).toBeVisible();
  expect(createBodies[0]).toMatchObject({
    agent_id: agentId,
    snapshot_hash: savedDraft.snapshot_hash,
    scenario_count: 1,
    scenario_ids: [scenario.scenario_id],
    utterance_model_id: "qa-utterance",
  });

  await page.getByRole("button", { name: "Apply to draft" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Suggestion applied to draft r3. It was not published." })).toBeVisible();
  await page.getByRole("button", { name: "Dismiss" }).click();
  await expect(page.getByText("Suggestion dismissed. The draft and published version were not changed.")).toBeVisible();

  await page.getByRole("button", { name: "Run selected scenarios" }).click();
  await expect(page.getByRole("button", { name: "Cancel job" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel job" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Cancelled" })).toBeVisible();
  expect(createBodies).toHaveLength(2);
  expect(requests.some((item) => item.method === "POST" && item.path.endsWith("/cancel"))).toBe(true);
  expect(requests.some((item) => item.path.endsWith("/suggestions/qa_sug_apply/apply"))).toBe(true);
  expect(requests.some((item) => item.path.endsWith("/suggestions/qa_sug_dismiss/dismiss"))).toBe(true);
  expect(publishCalls).toHaveLength(0);
});
