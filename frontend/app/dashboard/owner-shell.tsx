"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/components/ui/utils";

export type OwnerNavSection = "dashboard" | "agents" | null;

export function OwnerShell({
  children,
  active,
  email,
}: {
  children: ReactNode;
  active: OwnerNavSection;
  email?: string;
}) {
  const [signOutError, setSignOutError] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  async function signOut() {
    setSigningOut(true);
    setSignOutError(null);
    try {
      await api<void>("/auth/logout", { method: "POST" });
      window.location.assign("/login");
    } catch (error) {
      setSignOutError(error instanceof ApiError ? error.message : "Could not sign out. Try again.");
      setSigningOut(false);
    }
  }

  return (
    <div className="min-h-dvh md:flex">
      <aside className="sticky top-0 z-20 hidden h-dvh w-64 shrink-0 flex-col border-r border-border bg-card md:flex">
        <Link href="/dashboard" className="flex min-h-16 items-center gap-3 px-6 text-sm font-semibold tracking-tight focus-visible:outline-offset-4">
          <span aria-hidden="true" className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary text-sm font-bold text-primary-foreground">V</span>
          <span>Voice Agent Studio</span>
        </Link>
        <nav aria-label="Owner" className="flex flex-col gap-1 px-3 py-5">
          <Link
            href="/dashboard"
            aria-current={active === "dashboard" ? "page" : undefined}
            className={cn(
              "rounded-xl px-3 py-2.5 text-sm font-medium transition-colors hover:bg-muted focus-visible:outline-offset-2",
              active === "dashboard" ? "bg-muted text-foreground" : "text-muted-foreground",
            )}
          >
            Dashboard
          </Link>
          <Link
            href="/dashboard#agents"
            aria-current={active === "agents" ? "page" : undefined}
            className={cn(
              "rounded-xl px-3 py-2.5 text-sm font-medium transition-colors hover:bg-muted focus-visible:outline-offset-2",
              active === "agents" ? "bg-muted text-foreground" : "text-muted-foreground",
            )}
          >
            Agents
          </Link>
        </nav>
        <div className="mt-auto border-t border-border p-4">
          {email ? <p className="mb-3 truncate px-2 text-sm text-muted-foreground" title={email}>{email}</p> : null}
          <Button variant="ghost" className="w-full justify-start" onClick={signOut} disabled={signingOut}>
            {signingOut ? "Signing out…" : "Sign out"}
          </Button>
        </div>
      </aside>

      <div className="flex min-h-dvh min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 border-b border-border bg-background/95 backdrop-blur-sm md:hidden">
          <div className="flex min-h-16 items-center justify-between gap-3 px-4">
            <Link href="/dashboard" className="flex min-w-0 items-center gap-3 rounded-xl text-sm font-semibold tracking-tight focus-visible:outline-offset-4">
              <span aria-hidden="true" className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary text-sm font-bold text-primary-foreground">V</span>
              <span className="truncate">{active === "agents" ? "Agents" : active === "dashboard" ? "Dashboard" : "Voice Agent Studio"}</span>
            </Link>
            <Button
              variant="ghost"
              size="sm"
              className="size-9 p-0"
              aria-label={mobileMenuOpen ? "Close navigation menu" : "Open navigation menu"}
              aria-expanded={mobileMenuOpen}
              aria-controls="owner-mobile-menu"
              onClick={() => setMobileMenuOpen((open) => !open)}
            >
              {mobileMenuOpen ? (
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="size-5">
                  <path strokeLinecap="round" d="m6 6 12 12M18 6 6 18" />
                </svg>
              ) : (
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="size-5">
                  <path strokeLinecap="round" d="M4 7h16M4 12h16M4 17h16" />
                </svg>
              )}
            </Button>
          </div>
          {mobileMenuOpen ? (
            <div id="owner-mobile-menu" className="border-t border-border px-4 py-3">
              <nav aria-label="Owner" className="flex flex-col gap-1">
                <Link
                  href="/dashboard"
                  aria-current={active === "dashboard" ? "page" : undefined}
                  onClick={() => setMobileMenuOpen(false)}
                  className={cn(
                    "rounded-xl px-3 py-2.5 text-sm font-medium transition-colors hover:bg-muted focus-visible:outline-offset-2",
                    active === "dashboard" ? "bg-muted text-foreground" : "text-muted-foreground",
                  )}
                >
                  Dashboard
                </Link>
                <Link
                  href="/dashboard#agents"
                  aria-current={active === "agents" ? "page" : undefined}
                  onClick={() => setMobileMenuOpen(false)}
                  className={cn(
                    "rounded-xl px-3 py-2.5 text-sm font-medium transition-colors hover:bg-muted focus-visible:outline-offset-2",
                    active === "agents" ? "bg-muted text-foreground" : "text-muted-foreground",
                  )}
                >
                  Agents
                </Link>
              </nav>
              <div className="mt-3 border-t border-border pt-3">
                {email ? <p className="truncate px-3 pb-2 text-sm text-muted-foreground" title={email}>{email}</p> : null}
                <Button variant="ghost" className="w-full justify-start" onClick={signOut} disabled={signingOut}>
                  {signingOut ? "Signing out…" : "Sign out"}
                </Button>
              </div>
            </div>
          ) : null}
        </header>

        <div className="flex min-w-0 flex-1 flex-col">
          {signOutError ? (
            <p role="alert" className="mx-auto mt-4 w-full max-w-7xl px-4 text-sm text-destructive sm:px-6 lg:px-8">
              {signOutError}
            </p>
          ) : null}
          {children}
        </div>
      </div>
    </div>
  );
}
