import type { ReactNode } from "react";

export type LoadState<T> =
  | { kind: "loading" }
  | { kind: "failed"; message: string }
  | { kind: "ready"; data: T };

export type Notice =
  | { kind: "success"; message: string }
  | { kind: "error"; message: string }
  | null;

export function errorDetails(error: unknown, fallback: string): { message: string; status?: number } {
  if (typeof error !== "object" || error === null) {
    return { message: fallback };
  }

  const message =
    "message" in error && typeof error.message === "string" && error.message.length > 0
      ? error.message
      : fallback;
  const status = "status" in error && typeof error.status === "number" ? error.status : undefined;

  return { message, status };
}

export function AdminPage({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby="admin-page-title">
      <header className="mb-7 max-w-3xl">
        <h2 id="admin-page-title" className="text-2xl font-semibold tracking-tight sm:text-3xl">
          {title}
        </h2>
        <p className="mt-2 text-sm leading-6 text-muted-foreground sm:text-base">{description}</p>
      </header>
      {children}
    </section>
  );
}

export function AdminLoading({ label }: { label: string }) {
  return (
    <div className="rounded-lg border border-border bg-card p-5" aria-busy="true" aria-live="polite">
      <p className="text-sm text-muted-foreground">{label}</p>
      <div className="mt-5 space-y-3" aria-hidden="true">
        <div className="h-4 w-2/3 animate-pulse rounded bg-muted motion-reduce:animate-none" />
        <div className="h-4 w-full animate-pulse rounded bg-muted motion-reduce:animate-none" />
        <div className="h-4 w-1/2 animate-pulse rounded bg-muted motion-reduce:animate-none" />
      </div>
    </div>
  );
}

export function AdminLoadError({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-5" role="alert">
      <p className="font-medium text-destructive">{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-4 inline-flex min-h-10 items-center justify-center rounded-md border border-border bg-background px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60"
      >
        Try again
      </button>
    </div>
  );
}

export function AdminNotice({ notice }: { notice: Notice }) {
  if (!notice) return null;

  const style =
    notice.kind === "error"
      ? "border-destructive/30 bg-destructive/5 text-destructive"
      : "border-primary/30 bg-primary/5 text-foreground";

  return (
    <p
      className={`rounded-md border px-4 py-3 text-sm ${style}`}
      role={notice.kind === "error" ? "alert" : "status"}
      aria-live={notice.kind === "error" ? "assertive" : "polite"}
    >
      {notice.message}
    </p>
  );
}

export function formatDateTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;

  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function formatCount(value: number) {
  return new Intl.NumberFormat().format(value);
}

export const inputClassName =
  "min-h-11 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60";

export const primaryButtonClassName =
  "inline-flex min-h-11 items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow-sm transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60";
