import { describe, expect, test } from "bun:test";
import { formatDuration, wallClockFromPerformance } from "./format.ts";

describe("formatDuration", () => {
  test("formats sub-hour durations as minutes and seconds", () => {
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(5)).toBe("0:05");
    expect(formatDuration(65)).toBe("1:05");
    expect(formatDuration(3599)).toBe("59:59");
  });

  test("formats hour-length durations with zero-padded minutes and seconds", () => {
    expect(formatDuration(3600)).toBe("1:00:00");
    expect(formatDuration(3725)).toBe("1:02:05");
    expect(formatDuration(36000)).toBe("10:00:00");
  });

  test("floors fractional seconds", () => {
    expect(formatDuration(0.9)).toBe("0:00");
    expect(formatDuration(59.99)).toBe("0:59");
    expect(formatDuration(65.99)).toBe("1:05");
  });

  test("treats negative and non-finite input as zero", () => {
    expect(formatDuration(-1)).toBe("0:00");
    expect(formatDuration(-0.5)).toBe("0:00");
    expect(formatDuration(Number.NaN)).toBe("0:00");
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe("0:00");
    expect(formatDuration(Number.NEGATIVE_INFINITY)).toBe("0:00");
  });
});

describe("wallClockFromPerformance", () => {
  test("subtracts the age of the performance timestamp from the wall clock", () => {
    const wall = wallClockFromPerformance(1000, 1500, 10_000);
    expect(wall.getTime()).toBe(9500);
  });

  test("treats a future performance timestamp as now", () => {
    expect(wallClockFromPerformance(1200, 1000, 10_000).getTime()).toBe(10_000);
  });

  test("keeps millisecond precision", () => {
    expect(wallClockFromPerformance(1000, 1001.5, 20_000).getTime()).toBe(19_998);
  });

  test("falls back to now when a timestamp is not finite", () => {
    expect(wallClockFromPerformance(Number.NaN, 1000, 10_000).getTime()).toBe(10_000);
    expect(wallClockFromPerformance(0, Number.POSITIVE_INFINITY, 10_000).getTime()).toBe(10_000);
    expect(wallClockFromPerformance(0, 1000, Number.NaN).getTime()).toBe(0);
  });
});
