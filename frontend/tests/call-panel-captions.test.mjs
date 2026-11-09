import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

function visibleText(region) {
  const walker = region.ownerDocument.createTreeWalker(region, region.ownerDocument.defaultView.NodeFilter.SHOW_TEXT);
  const parts = [];
  while (walker.nextNode()) {
    const text = walker.currentNode.nodeValue?.trim();
    if (text) parts.push(text);
  }
  return parts.join(" ");
}

async function withActiveCall(run) {
  const dom = new JSDOM("<!doctype html><html><body><main id=\"root\"></main></body></html>", {
    url: "https://voice.test/",
  });
  const { window } = dom;
  const rootContainer = window.document.getElementById("root");
  const globalNames = [
    "window",
    "document",
    "navigator",
    "Node",
    "Element",
    "HTMLElement",
    "Event",
    "MouseEvent",
    "MessageEvent",
    "MutationObserver",
    "RTCPeerConnection",
    "fetch",
    "IS_REACT_ACT_ENVIRONMENT",
  ];
  const previousGlobals = new Map(globalNames.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  const setGlobal = (name, value) => Object.defineProperty(globalThis, name, {
    configurable: true,
    writable: true,
    value,
  });
  let act;
  let root;

  const mediaTrack = { enabled: true, stop() {} };
  const mediaStream = {
    getAudioTracks: () => [mediaTrack],
    getTracks: () => [mediaTrack],
  };
  Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
  Object.defineProperty(window.navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: async () => mediaStream },
  });
  window.HTMLMediaElement.prototype.pause = () => {};
  window.HTMLMediaElement.prototype.play = async () => {};

  class ExternalDataChannel extends window.EventTarget {
    readyState = "open";
    onmessage = null;

    send() {}
    close() {
      this.readyState = "closed";
    }
    dispatchEvent(event) {
      if (event.type === "message") this.onmessage?.call(this, event);
      return super.dispatchEvent(event);
    }
    deliverCaption(caption) {
      this.dispatchEvent(new window.MessageEvent("message", { data: JSON.stringify({ type: "caption", ...caption }) }));
    }
  }

  const peers = [];
  class ExternalPeerConnection {
    connectionState = "new";
    signalingState = "stable";
    localDescription = null;
    dataChannel = null;

    constructor() {
      peers.push(this);
    }

    addTrack() {}
    createDataChannel(label) {
      assert.equal(label, "caller-control");
      this.dataChannel = new ExternalDataChannel();
      return this.dataChannel;
    }
    async createOffer(options) {
      if (options?.iceRestart) throw new Error("Simulated browser restart-offer failure.");
      return { type: "offer", sdp: "v=0\\\\r\\\\n" };
    }
    async setLocalDescription(description) {
      this.localDescription = description;
    }
    async setRemoteDescription() {
      this.changeConnectionState("connected");
    }
    changeConnectionState(state) {
      this.connectionState = state;
      this.onconnectionstatechange?.();
    }
    close() {
      this.connectionState = "closed";
      this.signalingState = "closed";
    }
  }

  const mockFetch = async (input) => {
    const url = new URL(typeof input === "string" ? input : input.url, window.location.href);
    if (url.pathname === "/api/signaling/ice-servers") {
      return new Response(JSON.stringify({ iceServers: [{ urls: "stun:voice.test" }] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.pathname === "/api/signaling/offer") {
      return new Response(JSON.stringify({
        type: "answer",
        sdp: "v=0\\r\\n",
        pc_id: "test-peer",
        renegotiation_token: "r".repeat(32),
      }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    throw new Error(`Unexpected network request: ${url.pathname}`);
  };

  setGlobal("window", window);
  setGlobal("document", window.document);
  setGlobal("navigator", window.navigator);
  setGlobal("Node", window.Node);
  setGlobal("Element", window.Element);
  setGlobal("HTMLElement", window.HTMLElement);
  setGlobal("Event", window.Event);
  setGlobal("MouseEvent", window.MouseEvent);
  setGlobal("MessageEvent", window.MessageEvent);
  setGlobal("MutationObserver", window.MutationObserver);
  setGlobal("RTCPeerConnection", ExternalPeerConnection);
  setGlobal("fetch", mockFetch);
  setGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  window.RTCPeerConnection = ExternalPeerConnection;
  window.fetch = mockFetch;

  try {
    const react = await import("react");
    act = react.act;
    const { createRoot } = await import("react-dom/client");
    const { CallPanel } = await import("../components/call-panel.tsx");

    await act(async () => {
      root = createRoot(rootContainer);
      root.render(react.createElement(CallPanel, {
        title: "Reservations",
        createCall: async () => ({
          session_id: "123e4567-e89b-42d3-a456-426614174000",
          signaling_token: "s".repeat(32),
        }),
      }));
    });

    const startButton = [...rootContainer.querySelectorAll("button")]
      .find((button) => button.textContent?.trim() === "Start call");
    assert.ok(startButton, "the rendered call panel offers the Start call action");

    await act(async () => {
      startButton.click();
    });
    const deadline = Date.now() + 2_000;
    const isListening = () => [...rootContainer.querySelectorAll('[role="status"]')]
      .some((status) => status.textContent?.includes("Listening"));
    while (!isListening() && Date.now() < deadline) {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    }
    assert.ok(peers[0]?.dataChannel, "the simulated browser opened a data channel");
    assert.ok(isListening(), "the call became active before caption events were delivered");

    await run({ rootContainer, dataChannel: peers[0].dataChannel, peer: peers[0], act });
  } finally {
    if (root && act) await act(async () => root.unmount());
    dom.window.close();
    for (const [name, descriptor] of previousGlobals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  }
}

test("CallPanel keeps interleaved partial captions as two final turns", async () => {
  await withActiveCall(async ({ rootContainer, dataChannel, act }) => {
    await act(async () => {
      dataChannel.deliverCaption({ role: "assistant", text: "Your booking is", final: false });
      dataChannel.deliverCaption({ role: "user", text: "okay", final: false });
      dataChannel.deliverCaption({ role: "assistant", text: "Your booking is confirmed.", final: true });
      dataChannel.deliverCaption({ role: "user", text: "okay", final: true });
    });

    const transcript = rootContainer.querySelector('[aria-label="Live captions"]');
    assert.ok(transcript, "the rendered transcript is exposed as the Live captions region");
    assert.equal(
      visibleText(transcript),
      "Agent Your booking is confirmed. You okay",
      "interleaved partials must resolve to one complete assistant turn and one complete user turn",
    );
    assert.equal(
      transcript.querySelectorAll('[aria-label="streaming speech"]').length,
      0,
      "the visible transcript must not retain an interim-caption marker",
    );
  });
});

test("CallPanel preserves captions but stops streaming them after transport failure", async () => {
  await withActiveCall(async ({ rootContainer, dataChannel, peer, act }) => {
    await act(async () => {
      dataChannel.deliverCaption({ role: "assistant", text: "Your booking is", final: false });
    });

    const transcript = rootContainer.querySelector('[aria-label="Live captions"]');
    assert.ok(transcript, "the rendered transcript is exposed as the Live captions region");
    assert.equal(visibleText(transcript), "Agent Your booking is");
    assert.equal(transcript.querySelectorAll('[aria-label="streaming speech"]').length, 1);

    await act(async () => {
      peer.changeConnectionState("failed");
    });
    const deadline = Date.now() + 2_000;
    const isInterrupted = () => [...rootContainer.querySelectorAll('[role="status"]')]
      .some((status) => status.textContent?.includes("Call interrupted"));
    while (!isInterrupted() && Date.now() < deadline) {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    }

    assert.ok(isInterrupted(), "failed restart negotiation moves the rendered call panel to its failed state");
    assert.equal(visibleText(transcript), "Agent Your booking is", "the interim caption text remains visible");
    assert.equal(
      transcript.querySelectorAll('[aria-label="streaming speech"]').length,
      0,
      "a failed call must retain the caption without leaving an active streaming marker",
    );
  });
});
