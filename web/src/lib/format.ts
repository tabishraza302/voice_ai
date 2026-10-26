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
