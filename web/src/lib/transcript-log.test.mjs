import { describe, expect, test } from "bun:test";
import { TranscriptLog } from "./transcript-log.ts";

function fakeClock(start = 10_000) {
  let current = start;
  return {
    now: () => current,
    advance(ms) {
      current += ms;
    },
  };
}

describe("TranscriptLog", () => {
  test("appends trimmed entries with the role, timestamp and a unique id", () => {
    const clock = fakeClock();
    const log = new TranscriptLog(clock.now);

    const first = log.append("user", "  hello there  ");
    const second = log.append("assistant", "hello there");

    expect(first).toEqual({ id: expect.any(String), role: "user", text: "hello there", at: 10_000 });
    expect(second).toEqual({ id: expect.any(String), role: "assistant", text: "hello there", at: 10_000 });
    expect(first.id).not.toBe(second.id);
    expect(log.entries).toEqual([first, second]);
  });

  test("ignores empty and whitespace-only text", () => {
    const log = new TranscriptLog(fakeClock().now);

    expect(log.append("user", "")).toBeNull();
    expect(log.append("assistant", "   \n\t ")).toBeNull();
    expect(log.entries).toHaveLength(0);
  });

  test("drops an identical role and text inside the dedupe window", () => {
    const clock = fakeClock();
    const log = new TranscriptLog(clock.now);

    expect(log.append("user", "hello")).not.toBeNull();
    clock.advance(1_199);
    expect(log.append("user", "hello")).toBeNull();
    expect(log.entries).toHaveLength(1);
  });

  test("accepts the same line again at exactly the window boundary", () => {
    const clock = fakeClock();
    const log = new TranscriptLog(clock.now);

    log.append("user", "hello");
    clock.advance(1_200);

    expect(log.append("user", "hello")).toEqual({ id: expect.any(String), role: "user", text: "hello", at: 11_200 });
    expect(log.entries).toHaveLength(2);
  });

  test("measures the dedupe window from the last accepted line", () => {
    const clock = fakeClock();
    const log = new TranscriptLog(clock.now);

    log.append("user", "hello");
    clock.advance(700);
    expect(log.append("user", "hello")).toBeNull();
    clock.advance(500);
    expect(log.append("user", "hello")).not.toBeNull();
  });

  test("keeps identical text from different roles", () => {
    const log = new TranscriptLog(fakeClock().now);

    expect(log.append("user", "hello")).not.toBeNull();
    expect(log.append("assistant", "hello")).not.toBeNull();
    expect(log.entries.map((entry) => entry.role)).toEqual(["user", "assistant"]);
  });

  test("reset clears entries and dedupe history so a new session starts clean", () => {
    const clock = fakeClock();
    const log = new TranscriptLog(clock.now);

    log.append("user", "hello");
    log.reset();

    expect(log.entries).toHaveLength(0);
    expect(log.append("user", "hello")).not.toBeNull();
  });

  test("honours a custom dedupe window", () => {
    const clock = fakeClock();
    const log = new TranscriptLog(clock.now, 500);

    log.append("user", "hello");
    clock.advance(500);

    expect(log.append("user", "hello")).not.toBeNull();
  });

  test("gives every accepted entry its own id", () => {
    const clock = fakeClock();
    const log = new TranscriptLog(clock.now);

    for (let index = 0; index < 20; index += 1) {
      log.append("user", `line ${index}`);
      clock.advance(2_000);
    }

    const ids = log.entries.map((entry) => entry.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
