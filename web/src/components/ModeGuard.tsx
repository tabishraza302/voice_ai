"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-state";
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  KeyRound,
  Layers,
  Loader2,
  Lock,
  ScrollText,
  Settings,
  Shield,
  ShieldAlert,
  ShieldCheck,
  User,
  Users,
} from "lucide-react";
import Link from "next/link";

const AUTH_ROUTES = [
  "/login",
  "/register",
  "/verify-email",
  "/forgot-password",
  "/reset-password",
];

export function ModeGuard({ children }: { children: React.ReactNode }) {
  const { state, isLocalMode, appMode } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  const isAuthRoute = AUTH_ROUTES.some(
    (route) => pathname === route || pathname.startsWith(`${route}/`)
  );
  // Unconditional hook execution for React hook ordering
  useEffect(() => {
    if (isLocalMode) return;
    if (isAuthRoute) return;

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
  }, [isLocalMode, isAuthRoute, state.status, router, pathname]);

  // In local offline mode, all routes render directly as the local workspace
  if (isLocalMode) {
    return <>{children}</>;
  }

  // Auth pages are guest/public and render directly
  if (isAuthRoute) {
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

    // Explicit mode isolation: Local studio agents and playground pages are disabled in SaaS
    if (
      pathname === "/agents" ||
      pathname.startsWith("/agents/") ||
      pathname === "/playground"
    ) {
      return (
        <div className="surface p-8 max-w-2xl mx-auto space-y-4 border-ink-800 text-center">
          <Bot className="w-10 h-10 text-limestone-500 mx-auto" />
          <h2 className="text-xl font-bold text-white">
            Local Studio Isolated
          </h2>
          <p className="text-sm text-limestone-400 leading-relaxed">
            Local agent management routes (<code className="text-copper-300">/api/agents</code>, <code className="text-copper-300">/api/prompts</code>) are loopback-only and are disabled in SaaS mode. Cloud agent flows and runner sessions are managed through authenticated platform APIs.
          </p>
          <div className="pt-2 flex justify-center gap-3">
            <Link href="/" className="button-secondary">
              Back to Overview
            </Link>
            <Link href="/settings" className="button-primary">
              Account Settings
            </Link>
          </div>
        </div>
      );
    }

    // Overview root (/) in SaaS mode: render Customer Dashboard without calling local ApiClient
    if (pathname === "/") {
      return (
        <div className="space-y-8 max-w-5xl">
          <header className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 border-b border-ink-800 pb-5">
            <div>
              <div className="flex items-center gap-2">
                <span className="px-2 py-0.5 rounded text-[11px] font-semibold bg-copper-950 text-copper-300 border border-copper-800 uppercase tracking-wider">
                  SaaS Mode
                </span>
                <span className="text-xs text-limestone-400">
                  Isolated Platform Control Plane
                </span>
              </div>
              <h1 className="text-2xl font-bold text-white mt-1">
                Welcome, {user.display_name}
              </h1>
            </div>
            <Link
              href="/settings"
              className="button-secondary text-xs self-start sm:self-auto inline-flex items-center gap-2"
            >
              <Settings className="w-3.5 h-3.5" />
              Settings
            </Link>
          </header>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="surface p-5 space-y-2 border-ink-800 bg-ink-900/60">
              <div className="flex items-center justify-between text-xs text-limestone-400">
                <span className="font-medium">Account Role</span>
                <span className="px-2 py-0.5 rounded text-[10px] font-semibold uppercase bg-ink-800 text-limestone-200">
                  {user.role}
                </span>
              </div>
              <div className="text-lg font-bold text-white">{user.email}</div>
              <div className="text-xs text-limestone-500">
                Verified: {new Date(user.email_verified_at).toLocaleDateString()}
              </div>
            </div>

            <div className="surface p-5 space-y-2 border-ink-800 bg-ink-900/60">
              <div className="flex items-center justify-between text-xs text-limestone-400">
                <span className="font-medium">Preferences</span>
                <span className="text-[11px] text-limestone-300">
                  {user.preferred_language.toUpperCase()}
                </span>
              </div>
              <div className="text-lg font-bold text-white">{user.timezone}</div>
              <div className="text-xs text-limestone-500">
                Low balance alerts: {user.low_balance_notifications ? "On" : "Off"}
              </div>
            </div>

            <div className="surface p-5 space-y-2 border-ink-800 bg-ink-900/60">
              <div className="flex items-center justify-between text-xs text-limestone-400">
                <span className="font-medium">Security</span>
                {user.mfa_enabled ? (
                  <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-moss-950 text-moss-300 border border-moss-800 flex items-center gap-1">
                    <ShieldCheck className="w-3 h-3" />
                    MFA Enabled
                  </span>
                ) : (
                  <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-ink-800 text-limestone-400">
                    Standard
                  </span>
                )}
              </div>
              <div className="text-lg font-bold text-white">Active Session</div>
              <div className="text-xs text-limestone-500">
                CSRF-protected opaque cookie session
              </div>
            </div>
          </div>

          {/* Administrator shortcuts if admin */}
          {user.role === "admin" && (
            <section className="space-y-4">
              <div className="flex items-center gap-2">
                <Shield className="w-5 h-5 text-copper-400" />
                <h2 className="text-base font-semibold text-white">
                  Administrator Console
                </h2>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <Link
                  href="/admin/users"
                  className="surface p-5 border-ink-800 hover:border-copper-700/60 transition-all flex items-center justify-between group"
                >
                  <div className="space-y-1">
                    <div className="flex items-center gap-2 text-sm font-semibold text-white">
                      <Users className="w-4 h-4 text-copper-400" />
                      User Accounts
                    </div>
                    <p className="text-xs text-limestone-400">
                      Inspect, suspend, or restore platform accounts
                    </p>
                  </div>
                  <ArrowRight className="w-4 h-4 text-limestone-500 group-hover:text-copper-300 transition-colors" />
                </Link>

                <Link
                  href="/admin/settings"
                  className="surface p-5 border-ink-800 hover:border-copper-700/60 transition-all flex items-center justify-between group"
                >
                  <div className="space-y-1">
                    <div className="flex items-center gap-2 text-sm font-semibold text-white">
                      <ShieldCheck className="w-4 h-4 text-copper-400" />
                      Admin Security
                    </div>
                    <p className="text-xs text-limestone-400">
                      Configure two-factor TOTP factors and step-up authorization
                    </p>
                  </div>
                  <ArrowRight className="w-4 h-4 text-limestone-500 group-hover:text-copper-300 transition-colors" />
                </Link>
              </div>
            </section>
          )}

          {/* Mode Isolation Notice */}
          <div className="surface p-4 border-ink-800 bg-ink-900/40 text-xs text-limestone-400 flex items-start gap-3">
            <Layers className="w-4 h-4 text-copper-400 shrink-0 mt-0.5" />
            <div className="space-y-1">
              <span className="font-medium text-limestone-200 block">
                Local Studio Isolation Active
              </span>
              <span>
                You are currently in SaaS mode. The local machine agent studio endpoints (<code className="text-copper-300 bg-ink-950 px-1 py-0.5 rounded font-mono text-[11px]">http://localhost:7860/api</code>) are never called from this interface.
              </span>
            </div>
          </div>
        </div>
      );
    }

    return <>{children}</>;
  }

  return null;
}
