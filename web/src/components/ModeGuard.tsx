"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-state";
import {
  Bot,
  KeyRound,
  Loader2,
  Lock,
  ScrollText,
  ShieldAlert,
} from "lucide-react";
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
  const { state, isLocalMode } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  const isUnauthenticatedRoute = UNAUTHENTICATED_ROUTES.some(
    (route) => pathname === route || pathname.startsWith(`${route}/`)
  );
  // Unconditional hook execution for React hook ordering
  useEffect(() => {
    if (isLocalMode) return;
    if (isUnauthenticatedRoute) return;

    const search = typeof window !== "undefined" ? window.location.search : "";
    const currentPath = `${pathname}${search}` || "/";

    if (state.status === "anonymous") {
      router.push(`/login?next=${encodeURIComponent(currentPath)}`);
    } else if (state.status === "mfa_required") {
      router.push(`/login?next=${encodeURIComponent(currentPath)}`);
    } else if (
      state.status === "mfa_enrollment_required" &&
      pathname !== "/admin/settings"
    ) {
      router.push("/admin/settings");
    }
  }, [isLocalMode, isUnauthenticatedRoute, state.status, router, pathname]);

  // In local offline mode, all routes render directly as the local workspace
  if (isLocalMode) {
    return <>{children}</>;
  }

  // Authentication and public guest pages render without an account
  if (isUnauthenticatedRoute) {
    return <>{children}</>;
  }

  // SaaS mode loading state
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

  // Anonymous in SaaS mode
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

  // Restricted MFA states
  if (state.status === "mfa_required") {
    return (
      <div className="surface mx-auto max-w-md p-6 text-center space-y-4">
        <KeyRound className="mx-auto h-10 w-10 text-copper-400" />
        <h2 className="text-lg font-semibold text-white">Two-Factor Authentication Required</h2>
        <p className="text-sm text-limestone-400">
          Enter your 6-digit authenticator code to proceed.
        </p>
        <Link href="/login" className="button-primary w-full">
          Complete Verification
        </Link>
      </div>
    );
  }

  if (state.status === "mfa_enrollment_required") {
    if (pathname === "/admin/settings") {
      return <>{children}</>;
    }
    return (
      <div className="surface mx-auto max-w-md p-6 text-center space-y-4">
        <ShieldAlert className="mx-auto h-10 w-10 text-copper-400" />
        <h2 className="text-lg font-semibold text-white">Administrator MFA Enrollment</h2>
        <p className="text-sm text-limestone-400">
          Administrator accounts require multi-factor authentication setup before accessing platform resources.
        </p>
        <Link href="/admin/settings" className="button-primary w-full">
          Set Up Authenticator
        </Link>
      </div>
    );
  }

  // Authenticated state in SaaS mode
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

    // Customer settings and admin routes render their owned views
    if (pathname === "/settings" || pathname.startsWith("/admin/")) {
      return <>{children}</>;
    }

    // Explicit mode isolation: Global base prompt is forbidden in SaaS
    if (pathname === "/base-prompt") {
      return (
        <div className="surface p-8 max-w-2xl mx-auto space-y-4 border-ink-800 text-center">
          <ScrollText className="w-10 h-10 text-limestone-500 mx-auto" />
          <h2 className="text-xl font-bold text-white">
            Global Instructions Not Available
          </h2>
          <p className="text-sm text-limestone-400 leading-relaxed">
            Shared global prompt control is restricted to local offline studio mode. In SaaS platform mode, voice agents operate with isolated tenant configurations.
          </p>
          <div className="pt-2">
            <Link href="/" className="button-secondary">
              Back to Overview
            </Link>
          </div>
        </div>
      );
    }

    // Voice Playground stays local-only; platform customers use the pinned text Chat Test.
    if (pathname === "/playground") {
      return (
        <div className="surface mx-auto max-w-2xl space-y-4 p-8 text-center">
          <Bot className="mx-auto h-10 w-10 text-copper-300" />
          <h2 className="text-xl font-semibold text-limestone-100">Voice Playground is local-only</h2>
          <p className="text-sm leading-6 text-limestone-400">
            Open an agent workspace to test a text conversation against its saved draft or published version.
          </p>
          <Link href="/agents" className="button-primary mx-auto">
            Open agents
          </Link>
        </div>
      );
    }

    return <>{children}</>;
  }

  return null;
}
