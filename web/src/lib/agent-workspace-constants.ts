import type { JsonObject } from "@/lib/platform-agent-client";

export const SETTINGS = [
  { key: "llm_model", label: "Runtime model ID", maxLength: 64 },
  { key: "stt_provider", label: "Speech recognition provider ID", maxLength: 32 },
  { key: "tts_voice_en", label: "English voice ID", maxLength: 32 },
  { key: "tts_voice_hi", label: "Hindi voice ID", maxLength: 32 },
] as const;

export type RuntimeSettingKey = typeof SETTINGS[number]["key"];
export type WorkspaceTab = "builder" | "runtime" | "publish";



export function getString(config: JsonObject, key: RuntimeSettingKey): string {
  return typeof config[key] === "string" ? (config[key] as string) : "";
}

export function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Date unavailable"
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}
