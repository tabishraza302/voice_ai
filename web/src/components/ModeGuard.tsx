"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-state";
import { Loader2, Lock, ShieldAlert } from "lucide-react";
import Link from "next/link";

const UNAUTHENTICATED_ROUTES = [
  "/login",
  "/register",
  "/verify-email",
  "/forgot-password",
  "/reset-password",
  "/public",
];

export function ModeGuard({ children }: { children: React.ReactNode }) {
  const { state, refreshSession } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  const isUnauthenticatedRoute = UNAUTHENTICATED_ROUTES.some(
    (route) => pathname === route || pathname.startsWith(`${route}/`)
  );

  useEffect(() => {
    if (isUnauthenticatedRoute) return;

    const search = typeof window !== "undefined" ? window.location.search : "";
    const currentPath = `${pathname}${search}` || "/";

    if (state.status === "anonymous") {
      router.replace(`/login?next=${encodeURIComponent(currentPath)}`);
    }
  }, [isUnauthenticatedRoute, state.status, router, pathname]);

  // Authentication and public guest pages render without an account
  if (isUnauthenticatedRoute) {
    return <>{children}</>;
  }

  if (state.status === "loading") {
    return (
      <div
        role="status"
        aria-live="polite"
        className="flex min-h-[50vh] flex-col items-center justify-center gap-3 text-limestone-400"
      >
        <Loader2 className="h-8 w-8 animate-spin text-copper-400" />
        <span className="text-sm font-medium">Loading platform workspace...</span>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div
        role="alert"
        className="surface mx-auto max-w-lg space-y-4 border-rust-800 bg-rust-950/40 p-6 text-center text-rust-200"
      >
        <ShieldAlert className="mx-auto h-10 w-10 text-rust-400" />
        <h2 className="text-lg font-semibold text-white">Platform Unavailable</h2>
        <p className="text-sm text-rust-200">{state.message}</p>
        <button
          type="button"
          onClick={() => void refreshSession()}
          className="button-secondary mx-auto"
        >
          Try again
        </button>
      </div>
    );
  }

  if (state.status === "anonymous") {
    return (
      <div className="surface mx-auto max-w-md p-6 text-center space-y-4">
        <Lock className="mx-auto h-10 w-10 text-copper-400" />
        <h2 className="text-lg font-semibold text-white">Authentication Required</h2>
        <p className="text-sm text-limestone-400">
          Sign in with your customer or administrator account to access platform resources.
        </p>
        <Link
          href={`/login?next=${encodeURIComponent(pathname || "/")}`}
          className="button-primary w-full"
        >
          Sign in
        </Link>
      </div>
    );
  }


  if (state.status === "authenticated") {
    const user = state.user;

    if (user.status === "suspended") {
      return (
        <div
          role="alert"
          className="surface mx-auto max-w-lg p-6 border-rust-800 bg-rust-950/40 text-center space-y-3 text-rust-200"
        >
          <ShieldAlert className="mx-auto h-10 w-10 text-rust-400" />
          <h2 className="text-lg font-semibold text-white">Account Suspended</h2>
          <p className="text-sm text-rust-200">
            This account has been suspended by an administrator. Please contact support.
          </p>
        </div>
      );
    }

    return <>{children}</>;
  }

  return null;
}
