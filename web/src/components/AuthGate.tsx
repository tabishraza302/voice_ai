"use client";

import React, { useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth-state";
import { ShieldAlert, Loader2 } from "lucide-react";
import Link from "next/link";

interface AuthGateProps {
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

export function AuthGate({ children, fallback }: AuthGateProps) {
  const { state, isLocalMode } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  useEffect(() => {
    if (isLocalMode) return;
    const search = typeof window !== "undefined" ? window.location.search : "";
    const currentPath = `${pathname}${search}` || "/";

    if (state.status === "anonymous") {
      const loginUrl = `/login?next=${encodeURIComponent(currentPath)}`;
      router.push(loginUrl);
    } else if (state.status === "mfa_required") {
      router.push(`/login?next=${encodeURIComponent(currentPath)}`);
    } else if (state.status === "mfa_enrollment_required") {
      router.push("/admin/settings");
    }
  }, [isLocalMode, state.status, router, pathname]);

  // In local offline mode, all routes are open and bypassed
  if (isLocalMode) {
    return <>{children}</>;
  }
  if (state.status === "loading") {
    return (
      fallback || (
        <div
          role="status"
          aria-live="polite"
          className="flex min-h-[50vh] flex-col items-center justify-center gap-3 text-limestone-400"
        >
          <Loader2 className="h-8 w-8 animate-spin text-copper-400" />
          <span className="text-sm font-medium">Checking authentication...</span>
        </div>
      )
    );
  }

  if (state.status === "anonymous") {
    return (
      fallback || (
        <div className="surface mx-auto max-w-md p-6 text-center space-y-4">
          <ShieldAlert className="mx-auto h-10 w-10 text-copper-400" />
          <h2 className="text-lg font-semibold text-white">Sign In Required</h2>
          <p className="text-sm text-limestone-400">
            You must be signed in to access this workspace.
          </p>
          <Link
            href={`/login?next=${encodeURIComponent(pathname || "/")}`}
            className="button-primary w-full"
          >
            Go to Sign In
          </Link>
        </div>
      )
    );
  }

  if (state.status === "mfa_required") {
    return (
      <div className="surface mx-auto max-w-md p-6 text-center space-y-4">
        <ShieldAlert className="mx-auto h-10 w-10 text-copper-400" />
        <h2 className="text-lg font-semibold text-white">MFA Verification Required</h2>
        <p className="text-sm text-limestone-400">
          Two-factor authentication is required to access your account.
        </p>
        <Link href="/login" className="button-primary w-full">
          Complete Verification
        </Link>
      </div>
    );
  }

  if (state.status === "mfa_enrollment_required") {
    return (
      <div className="surface mx-auto max-w-md p-6 text-center space-y-4">
        <ShieldAlert className="mx-auto h-10 w-10 text-copper-400" />
        <h2 className="text-lg font-semibold text-white">MFA Enrollment Required</h2>
        <p className="text-sm text-limestone-400">
          Admin accounts require two-factor authentication setup before accessing resources.
        </p>
        <Link href="/admin/settings" className="button-primary w-full">
          Set Up Authenticator
        </Link>
      </div>
    );
  }

  if (state.status === "authenticated") {
    if (state.user.status === "suspended") {
      return (
        <div
          role="alert"
          className="surface mx-auto max-w-lg p-6 border-rust-800 bg-rust-950/40 text-center space-y-3 text-rust-200"
        >
          <ShieldAlert className="mx-auto h-10 w-10 text-rust-400" />
          <h2 className="text-lg font-semibold text-white">Account Suspended</h2>
          <p className="text-sm text-rust-200">
            This account has been suspended. Please contact your system administrator.
          </p>
        </div>
      );
    }

    return <>{children}</>;
  }

  return null;
}
