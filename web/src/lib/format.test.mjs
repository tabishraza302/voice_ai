import { describe, expect, test } from "bun:test";
import { formatDuration } from "./format.ts";

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
