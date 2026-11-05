import type { Agent } from "./types";

export const statusLabels: Record<Agent["status"], string> = {
  draft: "Draft",
  published: "Published",
  suspended: "Suspended",
};

export function statusVariant(status: Agent["status"]): "muted" | "success" | "warning" {
  if (status === "published") return "success";
  if (status === "suspended") return "warning";
  return "muted";
}
