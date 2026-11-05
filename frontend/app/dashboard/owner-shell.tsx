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
    <div className="min-h-dvh">
      <header className="sticky top-0 z-20 border-b bg-background/95 backdrop-blur-sm">
        <div className="mx-auto flex min-h-16 max-w-7xl items-center justify-between gap-2 px-4 sm:gap-4 sm:px-6 lg:px-8">
          <Link href="/dashboard" className="flex shrink-0 items-center gap-3 rounded-xl text-sm font-semibold tracking-tight focus-visible:outline-offset-4">
            <span aria-hidden="true" className="grid size-9 place-items-center rounded-xl bg-primary text-sm font-bold text-primary-foreground">V</span>
            <span className="hidden sm:inline">Voice Agent Studio</span>
          </Link>
          <nav aria-label="Owner" className="flex items-center gap-1">
            <Link
              href="/dashboard"
              aria-current={active === "dashboard" ? "page" : undefined}
              className={cn(
                "rounded-xl px-2 py-2 text-xs font-medium transition-colors hover:bg-muted sm:px-3 sm:text-sm",
                active === "dashboard" ? "bg-muted text-foreground" : "text-muted-foreground",
              )}
            >
              Dashboard
            </Link>
            <Link
              href="/dashboard#agents"
              aria-current={active === "agents" ? "page" : undefined}
              className={cn(
                "rounded-xl px-2 py-2 text-xs font-medium transition-colors hover:bg-muted sm:px-3 sm:text-sm",
                active === "agents" ? "bg-muted text-foreground" : "text-muted-foreground",
              )}
            >
              Agents
            </Link>
          </nav>
          <div className="flex shrink-0 items-center gap-2">
            {email ? <span className="hidden max-w-48 truncate text-sm text-muted-foreground md:inline">{email}</span> : null}
            <Button variant="ghost" size="sm" onClick={signOut} disabled={signingOut}>
              {signingOut ? "Signing out…" : "Sign out"}
            </Button>
          </div>
        </div>
      </header>
      {signOutError ? (
        <p role="alert" className="mx-auto mt-4 max-w-7xl px-4 text-sm text-destructive sm:px-6 lg:px-8">
          {signOutError}
        </p>
      ) : null}
      {children}
    </div>
  );
}
