const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 3600;

function pad(value: number): string {
  return value.toString().padStart(2, "0");
}

/**
 * Formats a duration as `m:ss`, or `h:mm:ss` once it reaches an hour.
 * Negative and non-finite input is treated as zero so a live timer never
 * renders `NaN:NaN`.
 */
export function formatDuration(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) return "0:00";
  const seconds = Math.floor(totalSeconds);
  const hours = Math.floor(seconds / SECONDS_PER_HOUR);
  const minutes = Math.floor((seconds % SECONDS_PER_HOUR) / SECONDS_PER_MINUTE);
  const remainder = seconds % SECONDS_PER_MINUTE;
  if (hours === 0) return `${minutes}:${pad(remainder)}`;
  return `${hours}:${pad(minutes)}:${pad(remainder)}`;
}

/**
 * Converts a `performance.now()` instant into the wall-clock `Date` at which
 * it happened. Performance timestamps are monotonic and immune to system
 * clock changes, so they are the right source for "when did the user start
 * speaking"; the transcript still needs a clock time to display.
 *
 * An onset slightly in the future (clock skew between the two clocks) is
 * treated as "now" rather than as a negative age.
 */
export function wallClockFromPerformance(
  performanceAt: number,
  performanceNow: number,
  wallNow: number
): Date {
  if (
    !Number.isFinite(performanceAt) ||
    !Number.isFinite(performanceNow) ||
    !Number.isFinite(wallNow)
  ) {
    return new Date(Number.isFinite(wallNow) ? wallNow : 0);
  }
  const age = Math.max(0, performanceNow - performanceAt);
  return new Date(wallNow - age);
}
