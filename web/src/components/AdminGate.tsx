"use client";

import React from "react";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth-state";
import { ShieldX, ShieldAlert } from "lucide-react";
import Link from "next/link";

interface AdminGateProps {
  children: React.ReactNode;
}

export function AdminGate({ children }: AdminGateProps) {
  const { state } = useAuth();
  const pathname = usePathname();

  if (state.status === "mfa_enrollment_required") {
    if (pathname === "/admin/settings") {
      return <>{children}</>;
    }
    return (
      <div className="surface mx-auto max-w-md p-6 text-center space-y-4">
        <ShieldAlert className="mx-auto h-10 w-10 text-copper-400" />
        <h2 className="text-lg font-semibold text-white">MFA Enrollment Required</h2>
        <p className="text-sm text-limestone-400">
          Admin accounts require multi-factor authentication enrollment before accessing administrative tools.
        </p>
        <Link href="/admin/settings" className="button-primary w-full">
          Set Up Authenticator
        </Link>
      </div>
    );
  }

  if (state.status === "authenticated") {
    if (state.user.role !== "admin") {
      return (
        <div
          role="alert"
          className="surface mx-auto max-w-lg p-8 border-rust-800 bg-rust-950/40 text-center space-y-4 text-rust-200"
        >
          <ShieldX className="mx-auto h-12 w-12 text-rust-400" />
          <h2 className="text-xl font-bold text-white">403 Access Denied</h2>
          <p className="text-sm text-rust-200">
            You do not have administrative privileges to access this page. This route is restricted to platform administrators.
          </p>
          <div className="pt-2">
            <Link href="/" className="button-secondary">
              Back to Overview
            </Link>
          </div>
        </div>
      );
    }

    if (!state.user.mfa_enabled && pathname !== "/admin/settings") {
      return (
        <div className="surface mx-auto max-w-md p-6 text-center space-y-4">
          <ShieldAlert className="mx-auto h-10 w-10 text-copper-400" />
          <h2 className="text-lg font-semibold text-white">MFA Setup Required</h2>
          <p className="text-sm text-limestone-400">
            Admin accounts must enable multi-factor authentication.
          </p>
          <Link href="/admin/settings" className="button-primary w-full">
            Complete MFA Setup
          </Link>
        </div>
      );
    }

    return <>{children}</>;
  }

  // ModeGuard owns loading, error, anonymous, and MFA verification screens.
  return null;
}
