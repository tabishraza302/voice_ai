import { expect, test, type Page } from "@playwright/test";

const publicId = "guest-retry";
const disclosureVersion = "guest-disclosure-v1";
const publicCsrf = "public-csrf-test-only";

interface MockAdmission {
  session_id: string;
  state: string;
  signaling_url: string;
  join_token: string;
  join_expires_at: string;
  hard_end_deadline: string;
  csrf_token: string;
}

async function installBootstrap(page: Page): Promise<void> {
  await page.route(`**/api/v1/public/deployments/${publicId}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        public_id: publicId,
        agent_name: "Guest retry agent",
        agent_description: "",
        disclosure_version: disclosureVersion,
        disclosure_text: "This guest voice session follows the displayed disclosure.",
        csrf_token: publicCsrf,
      }),
    });
  });
}

async function openGuestPage(page: Page): Promise<void> {
  await page.goto(`/public/${publicId}`);
  await expect(page.getByText(`Disclosure version ${disclosureVersion}`)).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: "I have read and acknowledge the disclosure above." }),
  ).toBeVisible();
}

async function installFakeVoiceApis(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const audioTrack = { enabled: true, stop: () => {} };
    const stream = {
      getAudioTracks: () => [audioTrack],
      getTracks: () => [audioTrack],
    } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: async () => stream },
    });

    class FakePeerConnection {
      connectionState: RTCPeerConnectionState = "new";
      iceGatheringState: RTCIceGatheringState = "complete";
      localDescription: RTCSessionDescriptionInit | null = null;
      onconnectionstatechange: RTCPeerConnection["onconnectionstatechange"] = null;

      addTrack(): void {}

      addTransceiver(): void {}

      createDataChannel(): RTCDataChannel {
        return {
          readyState: "open",
          send: () => {},
          close: () => {},
        } as unknown as RTCDataChannel;
      }

      async createOffer(): Promise<RTCSessionDescriptionInit> {
        return { type: "offer", sdp: "offer-sdp" };
      }

      async setLocalDescription(description?: RTCSessionDescriptionInit): Promise<void> {
        this.localDescription = description ?? null;
      }

      async setRemoteDescription(): Promise<void> {
        this.connectionState = "connected";
        this.onconnectionstatechange?.call(
          this as unknown as RTCPeerConnection,
          new Event("connectionstatechange"),
        );
      }

      close(): void {
        this.connectionState = "closed";
      }
    }

    Object.defineProperty(window, "RTCPeerConnection", {
      configurable: true,
      value: FakePeerConnection,
    });
  });
}

test("a lost guest admission response replays one session and recovers its join after reload", async ({ page }) => {
  const admissions = new Map<string, { body: string; response: MockAdmission }>();
  const admissionRequests: Array<{ key: string; body: string }> = [];
  const joinRequests: Array<{ session_id?: string; join_token?: string }> = [];
  let createdSessions = 0;

  await installBootstrap(page);
  await installFakeVoiceApis(page);
  await page.route(`**/api/v1/public/deployments/${publicId}/sessions`, async (route) => {
    const key = route.request().headers()["idempotency-key"] ?? "";
    const body = route.request().postData() ?? "";
    admissionRequests.push({ key, body });

    let admission = admissions.get(key);
    if (admission && admission.body !== body) {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          error: { code: "IDEMPOTENCY_CONFLICT", message: "The admission payload changed." },
        }),
      });
      return;
    }

    if (!admission) {
      createdSessions += 1;
      admission = {
        body,
        response: {
          session_id: `00000000-0000-4000-8000-${String(createdSessions).padStart(12, "0")}`,
          state: "REGISTERED",
          signaling_url: "/mock-voice/join",
          join_token: `join-token-${createdSessions}`,
          join_expires_at: "2099-01-01T00:10:00Z",
          hard_end_deadline: "2099-01-01T00:20:00Z",
          csrf_token: `guest-csrf-${createdSessions}`,
        },
      };
      admissions.set(key, admission);
    }

    if (admissionRequests.length === 1) {
      await route.abort("failed");
      return;
    }

    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify(admission.response),
    });
  });
  await page.route("**/api/v1/guest/sessions/*/status", async (route) => {
    const sessionId = new URL(route.request().url()).pathname.split("/").at(-2) ?? "";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        session_id: sessionId,
        state: "REGISTERED",
        session_version: 1,
        created_at: "2026-10-30T00:00:00Z",
        hard_end_deadline: "2099-01-01T00:20:00Z",
        ended_at: null,
      }),
    });
  });
  await page.route("**/mock-voice/join", async (route) => {
    if (route.request().method() === "POST") {
      const body = JSON.parse(route.request().postData() ?? "{}") as {
        request_data?: { session_id?: string; join_token?: string };
      };
      if (body.request_data) joinRequests.push(body.request_data);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ pc_id: "guest-pc-1", sdp: "answer-sdp", type: "answer" }),
      });
      return;
    }
    await route.fulfill({ status: 200 });
  });

  await openGuestPage(page);
  const acknowledgement = page.getByRole("checkbox", {
    name: "I have read and acknowledge the disclosure above.",
  });
  await acknowledgement.check();
  await page.getByRole("button", { name: "Start voice session" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(admissionRequests).toHaveLength(1);

  const pendingStorage = await page.evaluate(() => {
    const values: string[] = [];
    for (let index = 0; index < sessionStorage.length; index += 1) {
      const key = sessionStorage.key(index);
      if (key) values.push(sessionStorage.getItem(key) ?? "");
    }
    return values.join("\n");
  });
  expect(pendingStorage).not.toContain(publicCsrf);

  await page.reload();
  const reloadedAcknowledgement = page.getByRole("checkbox", {
    name: "I have read and acknowledge the disclosure above.",
  });
  await expect(reloadedAcknowledgement).toBeVisible();
  await reloadedAcknowledgement.check();
  await page.getByRole("button", { name: "Start voice session" }).click();
  await expect(page.getByRole("status")).toContainText(
    "Connected. Speak naturally; live audio is active.",
  );

  expect(admissionRequests).toHaveLength(2);
  expect(admissionRequests[0]?.key).not.toBe("");
  expect(admissionRequests[1]).toEqual(admissionRequests[0]);
  expect(createdSessions).toBe(1);
  expect(joinRequests).toHaveLength(1);
  const replayedAdmission = admissions.get(admissionRequests[0]!.key)?.response;
  expect(joinRequests[0]).toMatchObject({
    session_id: replayedAdmission?.session_id,
    join_token: replayedAdmission?.join_token,
  });
});

test("a definitive guest admission rejection allows a new operation key", async ({ page }) => {
  const admissionRequests: Array<{ key: string; body: string }> = [];
  await installBootstrap(page);
  await page.route(`**/api/v1/public/deployments/${publicId}/sessions`, async (route) => {
    admissionRequests.push({
      key: route.request().headers()["idempotency-key"] ?? "",
      body: route.request().postData() ?? "",
    });
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        error: { code: "ADMISSION_REJECTED", message: "The guest admission was rejected." },
      }),
    });
  });

  await openGuestPage(page);
  await page.getByRole("checkbox", {
    name: "I have read and acknowledge the disclosure above.",
  }).check();
  await page.getByRole("button", { name: "Start voice session" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "The guest admission was rejected." })).toBeVisible();

  await page.reload();
  await page.getByRole("checkbox", {
    name: "I have read and acknowledge the disclosure above.",
  }).check();
  await page.getByRole("button", { name: "Start voice session" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "The guest admission was rejected." })).toBeVisible();

  expect(admissionRequests).toHaveLength(2);
  expect(admissionRequests[0]?.key).not.toBe("");
  expect(admissionRequests[1]?.key).not.toBe(admissionRequests[0]?.key);
  expect(admissionRequests[1]?.body).toBe(admissionRequests[0]?.body);
});

test("a finished session starts a new admission even when session storage was full", async ({ page }) => {
  const keys: string[] = [];
  await installBootstrap(page);
  await installFakeVoiceApis(page);
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("public-guest-session:")) throw new DOMException("full", "QuotaExceededError");
      return original.call(this, key, value);
    };
  });
  await page.route(`**/api/v1/public/deployments/${publicId}/sessions`, async (route) => {
    const key = route.request().headers()["idempotency-key"] ?? "";
    keys.push(key);
    if (keys.length > 1 && key === keys[0]) {
      await route.fulfill({ status: 409, json: { error: { code: "CONFLICT", message: "Session ended" } } });
      return;
    }
    await route.fulfill({ status: 201, json: {
      session_id: `00000000-0000-4000-8000-${String(keys.length).padStart(12, "0")}`,
      state: "REGISTERED", signaling_url: "/mock-voice/join", join_token: "join-token",
      join_expires_at: "2099-01-01T00:10:00Z", hard_end_deadline: "2099-01-01T00:20:00Z",
      csrf_token: "guest-csrf",
    } });
  });
  await page.route("**/api/v1/guest/sessions/*/status", async (route) => {
    await route.fulfill({ json: { state: "REGISTERED" } });
  });
  await page.route("**/api/v1/guest/sessions/*/end", async (route) => {
    await route.fulfill({ json: { state: "CLOSED" } });
  });
  await page.route("**/mock-voice/join", async (route) => {
    await route.fulfill({ json: { pc_id: "guest-pc", sdp: "answer-sdp", type: "answer" } });
  });

  await openGuestPage(page);
  const acknowledgement = page.getByRole("checkbox", { name: "I have read and acknowledge the disclosure above." });
  await acknowledgement.check();
  await page.getByRole("button", { name: "Start voice session" }).click();
  await expect(page.getByRole("status")).toContainText("Connected. Speak naturally; live audio is active.");
  await page.getByRole("button", { name: "End session" }).click();
  await page.getByRole("button", { name: "Start another session" }).click();
  await acknowledgement.check();
  await page.getByRole("button", { name: "Start voice session" }).click();
  await expect(page.getByRole("status")).toContainText("Connected. Speak naturally; live audio is active.");
  expect(keys).toHaveLength(2);
  expect(keys[1]).not.toBe(keys[0]);
});
