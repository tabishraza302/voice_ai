"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { api } from "../../lib/api";
import { AdminLoadError, AdminLoading, errorDetails } from "./admin-ui";
import { buttonClassName } from "../ui/button";
import type { AdminSession } from "./admin-types";

type GuardState =
  | { kind: "checking" }
  | { kind: "admin"; session: AdminSession }
  | { kind: "signed-out" }
  | { kind: "not-admin" }
  | { kind: "failed"; message: string };

const sections = [
  { href: "/admin/global-prompt", label: "Global prompt" },
  { href: "/admin/policy", label: "Policy" },
  { href: "/admin/users", label: "Users" },
  { href: "/admin/agents", label: "Agents" },
] as const;

function parseSession(value: unknown): AdminSession | null {
  if (typeof value !== "object" || value === null || !("role" in value) || !("email" in value)) {
    return null;
  }

  const role: unknown = value.role;
  const email: unknown = value.email;
  if ((role !== "admin" && role !== "user") || typeof email !== "string") return null;

  return { role, email };
}

export function AdminShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [retryKey, setRetryKey] = useState(0);
  const [guard, setGuard] = useState<GuardState>({ kind: "checking" });

  useEffect(() => {
    const controller = new AbortController();
    setGuard({ kind: "checking" });

    void api<unknown>("/auth/me", { signal: controller.signal })
      .then((value) => {
        const session = parseSession(value);
        if (!session) {
          setGuard({ kind: "failed", message: "The account response could not be read." });
          return;
        }

        setGuard(
          session.role === "admin"
            ? { kind: "admin", session }
            : { kind: "not-admin" },
        );
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const details = errorDetails(error, "We couldn't verify administrator access.");
        setGuard(
          details.status === 401
            ? { kind: "signed-out" }
            : { kind: "failed", message: details.message },
        );
      });

    return () => controller.abort();
  }, [retryKey]);

  if (guard.kind === "checking") {
    return (
      <main className="min-h-dvh bg-background px-4 py-10 text-foreground sm:px-6 lg:px-8">
        <div className="mx-auto max-w-7xl">
          <AdminLoading label="Checking administrator access…" />
        </div>
      </main>
    );
  }

  if (guard.kind === "signed-out") {
    return (
      <main className="min-h-dvh bg-background px-4 py-10 text-foreground sm:px-6 lg:px-8">
        <section className="mx-auto max-w-xl rounded-2xl border border-border bg-card p-6 sm:p-8">
          <h1 className="text-2xl font-semibold tracking-tight">Sign in to continue</h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            Administrator pages are available after you sign in with an administrator account.
          </p>
          <Link
            href="/login"
            className={buttonClassName("primary", "default", "mt-5")}
          >
            Go to sign in
          </Link>
        </section>
      </main>
    );
  }

  if (guard.kind === "not-admin") {
    return (
      <main className="min-h-dvh bg-background px-4 py-10 text-foreground sm:px-6 lg:px-8">
        <section className="mx-auto max-w-xl rounded-2xl border border-border bg-card p-6 sm:p-8" role="alert">
          <h1 className="text-2xl font-semibold tracking-tight">Administrator access required</h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            This account cannot open the administration console.
          </p>
        </section>
      </main>
    );
  }

  if (guard.kind === "failed") {
    return (
      <main className="min-h-dvh bg-background px-4 py-10 text-foreground sm:px-6 lg:px-8">
        <div className="mx-auto max-w-xl">
          <AdminLoadError message={guard.message} onRetry={() => setRetryKey((key) => key + 1)} />
        </div>
      </main>
    );
  }

  return (
    <div className="min-h-dvh bg-background text-foreground">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <header className="border-b border-border py-6 sm:py-8">
          <div className="flex flex-col gap-5 md:flex-row md:items-end md:justify-between">
            <div>
              <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Administration</h1>
              <p className="mt-1 text-sm text-muted-foreground">Signed in as {guard.session.email}</p>
            </div>
            <nav aria-label="Administration sections" className="-mx-1 flex gap-1 overflow-x-auto pb-1">
              {sections.map((section) => {
                const active = pathname === section.href || pathname.startsWith(`${section.href}/`);
                return (
                  <Link
                    key={section.href}
                    href={section.href}
                    aria-current={active ? "page" : undefined}
                    className={`inline-flex min-h-10 shrink-0 items-center rounded-xl px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 ${
                      active
                        ? "bg-muted text-foreground"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground"
                    }`}
                  >
                    {section.label}
                  </Link>
                );
              })}
            </nav>
          </div>
        </header>
        <main className="py-8 sm:py-10">{children}</main>
      </div>
    </div>
  );
}
