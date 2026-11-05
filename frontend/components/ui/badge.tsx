import type { HTMLAttributes } from "react";
import { cn } from "./utils";

type BadgeVariant = "muted" | "success" | "warning" | "danger";

const badgeClasses: Record<BadgeVariant, string> = {
  muted: "border-border bg-muted text-muted-foreground",
  success: "border-primary/25 bg-primary/10 text-primary",
  warning: "border-amber-700/25 bg-amber-700/10 text-amber-900 dark:text-amber-200",
  danger: "border-destructive/25 bg-destructive/10 text-destructive",
};

export function Badge({ className, variant = "muted", ...props }: HTMLAttributes<HTMLSpanElement> & { variant?: BadgeVariant }) {
  return (
    <span
      className={cn("inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-medium", badgeClasses[variant], className)}
      {...props}
    />
  );
}
