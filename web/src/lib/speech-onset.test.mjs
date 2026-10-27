import { describe, expect, test } from "bun:test";
import { SpeechOnsetTracker } from "./speech-onset.ts";

describe("SpeechOnsetTracker", () => {
  test("fires once the level has stayed above the threshold for the sustain window", () => {
    const tracker = new SpeechOnsetTracker({ threshold: 0.02, sustainMs: 80, releaseMs: 300 });

    expect(tracker.observe(0.005, 1000)).toBeNull();
    expect(tracker.observe(0.03, 1020)).toBeNull();
    expect(tracker.observe(0.03, 1080)).toBeNull();
    // Confirmed at 1100, but stamped when the level first rose: 1020.
    expect(tracker.observe(0.03, 1100)).toBe(1020);
  });

  test("does not fire again while the same speech continues", () => {
    const tracker = new SpeechOnsetTracker({ threshold: 0.02, sustainMs: 60, releaseMs: 200 });

    tracker.observe(0.03, 0);
    expect(tracker.observe(0.03, 60)).toBe(0);
    expect(tracker.observe(0.04, 200)).toBeNull();
    expect(tracker.observe(0.05, 500)).toBeNull();
  });

  test("a new turn's onset fires only after reset", () => {
    const tracker = new SpeechOnsetTracker({ threshold: 0.02, sustainMs: 60, releaseMs: 200 });

    tracker.observe(0.03, 0);
    expect(tracker.observe(0.03, 60)).toBe(0);
    expect(tracker.take()).toBe(0);

    // The dialog resets at the turn boundary; the next rise is a new turn.
    tracker.reset();
    expect(tracker.observe(0.03, 400)).toBeNull();
    expect(tracker.observe(0.03, 460)).toBe(400);
  });

  test("a pause shorter than the release window does not split one utterance", () => {
    const tracker = new SpeechOnsetTracker({ threshold: 0.02, sustainMs: 40, releaseMs: 250 });

    tracker.observe(0.03, 0);
    expect(tracker.observe(0.03, 40)).toBe(0);

    // Still inside the same utterance: the pause is shorter than the release.
    expect(tracker.observe(0.001, 100)).toBeNull();
    expect(tracker.observe(0.03, 200)).toBeNull();
    expect(tracker.observe(0.03, 300)).toBeNull();
  });

  test("does not hand a later turn an onset captured after this one was consumed", () => {
    const tracker = new SpeechOnsetTracker({ threshold: 0.02, sustainMs: 60, releaseMs: 200 });

    tracker.observe(0.03, 0);
    expect(tracker.observe(0.03, 60)).toBe(0);
    expect(tracker.take()).toBe(0);

    // A pause in the same utterance re-arms the detector. It must not leave
    // an onset behind for the next turn to inherit.
    expect(tracker.observe(0.001, 100)).toBeNull();
    expect(tracker.observe(0.001, 320)).toBeNull();
    expect(tracker.observe(0.03, 350)).toBeNull();
    expect(tracker.observe(0.03, 410)).toBeNull();

    tracker.reset();
    expect(tracker.observe(0.03, 500)).toBeNull();
    expect(tracker.observe(0.03, 560)).toBe(500);
  });

  test("take returns and clears the pending onset", () => {
    const tracker = new SpeechOnsetTracker({ threshold: 0.02, sustainMs: 20 });

    tracker.observe(0.03, 10);
    tracker.observe(0.03, 30);
    expect(tracker.take()).toBe(10);
    expect(tracker.take()).toBeNull();
  });

  test("clearPending drops an unused onset without re-arming the tracker", () => {
    const tracker = new SpeechOnsetTracker({ threshold: 0.02, sustainMs: 20, releaseMs: 100 });

    tracker.observe(0.03, 0);
    tracker.observe(0.03, 20);
    tracker.clearPending();

    expect(tracker.take()).toBeNull();
    // Still inside the utterance: no second onset.
    expect(tracker.observe(0.03, 60)).toBeNull();
    // Quiet long enough to re-arm, then a fresh onset.
    expect(tracker.observe(0.001, 100)).toBeNull();
    expect(tracker.observe(0.001, 200)).toBeNull();
    expect(tracker.observe(0.03, 300)).toBeNull();
    expect(tracker.observe(0.03, 320)).toBe(300);
  });

  test("reset clears pending, active speech and the release clock", () => {
    const tracker = new SpeechOnsetTracker({ threshold: 0.02, sustainMs: 20, releaseMs: 500 });

    tracker.observe(0.03, 0);
    tracker.observe(0.03, 20);
    tracker.reset();

    expect(tracker.take()).toBeNull();
    expect(tracker.observe(0.03, 40)).toBeNull();
    expect(tracker.observe(0.03, 60)).toBe(40);
  });

  test("ignores non-finite levels and timestamps", () => {
    const tracker = new SpeechOnsetTracker({ threshold: 0.02, sustainMs: 20 });

    expect(tracker.observe(Number.NaN, 0)).toBeNull();
    expect(tracker.observe(0.03, Number.NaN)).toBeNull();
    expect(tracker.observe(0.03, 0)).toBeNull();
    expect(tracker.observe(0.03, 20)).toBe(0);
  });

  test("a backwards clock starts a new measurement instead of firing early", () => {
    const tracker = new SpeechOnsetTracker({ threshold: 0.02, sustainMs: 50 });

    tracker.observe(0.03, 1000);
    expect(tracker.observe(0.03, 500)).toBeNull();
    expect(tracker.observe(0.03, 550)).toBe(500);
  });
});
