import { expect, test } from "@playwright/test";

const jobId = "00000000-0000-0000-0000-000000000012";
const stamp = "2099-01-01T00:00:00Z";

const session = {
  state: "authenticated",
  user: {
    id: "00000000-0000-0000-0000-000000000011",
    email: "generation-race@example.com",
    display_name: "Generation Race Test",
    role: "user",
    status: "active",
    email_verified_at: stamp,
    preferred_language: "en",
    timezone: "UTC",
    low_balance_notifications: false,
    mfa_enabled: false,
    created_at: stamp,
    updated_at: stamp,
  },
  session_id: "00000000-0000-0000-0000-000000000013",
  expires_at: stamp,
  csrf_token: "generation-race-csrf",
  step_up_expires_at: null,
};

const completedJob = {
  status: "COMPLETED",
  proposal: {
    name: "Loan guide",
    description: "Eligibility guidance.",
    persona_prompt: "Ask only for confirmed facts.",
    config: { flow: { schema_version: 1, steps: [] } },
  },
  unknown_business_facts: ["Confirm the customer's region."],
  validation_warnings: [],
  usage: { prompt_tokens: null, completion_tokens: null, total_tokens: null },
};

test("cancellation racing with generation completion reloads the proposal", async ({ page }) => {
  let cancellationRequested = false;
  let completionReads = 0;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname.endsWith("/auth/session")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(session) });
      return;
    }
    if (pathname.endsWith("/templates")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], next_cursor: null }),
      });
      return;
    }
    if (pathname.endsWith("/generation-models")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [] }) });
      return;
    }
    if (pathname.endsWith(`/generation-jobs/${jobId}/cancel`)) {
      cancellationRequested = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ job_id: jobId, status: "COMPLETED" }),
      });
      return;
    }
    if (pathname.endsWith(`/generation-jobs/${jobId}`)) {
      if (!cancellationRequested) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            status: "RUNNING",
            proposal: null,
            unknown_business_facts: [],
            validation_warnings: [],
            usage: null,
          }),
        });
        return;
      }
      completionReads += 1;
      if (completionReads === 1) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({
            error: {
              code: "SERVICE_UNAVAILABLE",
              message: "Temporary job detail failure",
              request_id: "generation-race",
              retryable: true,
              fields: [],
            },
          }),
        });
        return;
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(completedJob) });
      return;
    }
    await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: { code: "NOT_FOUND", message: "Unexpected test request" } }) });
  });

  await page.goto(`/agents/new?mode=generate&job=${jobId}`);
  await expect(page.getByRole("heading", { name: "RUNNING", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Cancel generation" }).click();
  await expect(page.getByRole("heading", { name: "COMPLETED", exact: true })).toBeVisible();
  await expect(page.getByText("Generation completed without a proposal. No agent draft was created.")).toHaveCount(0);
  const retryButton = page.getByRole("button", { name: "Retry status check" });
  const proposalHeading = page.getByRole("heading", { name: "Loan guide", exact: true });
  await expect.poll(async () => (await proposalHeading.isVisible()) || (await retryButton.isVisible())).toBe(true);
  if (await retryButton.isVisible()) await retryButton.click();
  await expect(proposalHeading).toBeVisible();
  await expect(page.getByText(/Usage: unknown prompt \+ unknown completion tokens, unknown total\./)).toBeVisible();
  await expect(page.getByText("Generation completed without a proposal. No agent draft was created.")).toHaveCount(0);
  expect(completionReads).toBeGreaterThanOrEqual(2);
});

test("a delayed status retry cannot replace the newly selected generation", async ({ page }) => {
  const nextJobId = "00000000-0000-0000-0000-000000000014";
  let jobReads = 0;
  const { promise: delayedResponse, resolve: releaseDelayedResponse } = Promise.withResolvers<void>();
  const { promise: delayedRetryStarted, resolve: signalDelayedRetry } = Promise.withResolvers<void>();
  await page.route("**/api/v1/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/auth/session")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(session) });
      return;
    }
    if (pathname.endsWith("/templates")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], next_cursor: null }),
      });
      return;
    }
    if (pathname.endsWith("/generation-models")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [{ id: "test-model", name: "Test model", description: "", supports_reasoning: false }],
        }),
      });
      return;
    }
    if (pathname.endsWith("/agents/generate")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ job_id: nextJobId, status: "RUNNING" }),
      });
      return;
    }
    if (pathname.endsWith(`/generation-jobs/${jobId}`)) {
      jobReads += 1;
      if (jobReads <= 2) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            status: "RUNNING",
            proposal: null,
            unknown_business_facts: [],
            validation_warnings: [],
            usage: null,
          }),
        });
        return;
      }
      if (jobReads === 3) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({
            error: {
              code: "SERVICE_UNAVAILABLE",
              message: "Temporary job detail failure",
              request_id: "generation-retry-race",
              retryable: true,
              fields: [],
            },
          }),
        });
        return;
      }
      signalDelayedRetry();
      await delayedResponse;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...completedJob,
          proposal: { ...completedJob.proposal, name: "Old job proposal" },
        }),
      });
      return;
    }
    if (pathname.endsWith(`/generation-jobs/${nextJobId}`)) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "RUNNING",
          proposal: null,
          unknown_business_facts: [],
          validation_warnings: [],
          usage: null,
        }),
      });
      return;
    }
    await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: { code: "NOT_FOUND", message: "Unexpected test request" } }) });
  });

  await page.goto(`/agents/new?mode=generate&job=${jobId}`);
  await expect(page.getByRole("button", { name: "Retry status check" })).toBeVisible();
  await page.getByRole("button", { name: "Retry status check" }).click();

  try {
    await delayedRetryStarted;
    await page.getByLabel("What should the agent do?").fill("Guide a customer through a loan application.");
    await page.getByLabel("Generation model").selectOption("test-model");
    await page.getByLabel("Token budget").fill("64");
    await page.getByRole("button", { name: "Generate proposal" }).click();
    await expect(page.getByText(`Job ID: ${nextJobId}`)).toBeVisible();
    await expect(page.getByRole("heading", { name: "RUNNING", exact: true })).toBeVisible();
  } finally {
    releaseDelayedResponse();
  }

  await expect(page.getByText(`Job ID: ${nextJobId}`)).toBeVisible();
  await expect(page.getByRole("heading", { name: "RUNNING", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Old job proposal", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save proposal as draft" })).toHaveCount(0);
});
