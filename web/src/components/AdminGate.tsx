"use client";

import React from "react";
import { useAuth } from "@/lib/auth-state";
import { ShieldX } from "lucide-react";
import Link from "next/link";

interface AdminGateProps {
  children: React.ReactNode;
}

export function AdminGate({ children }: AdminGateProps) {
  const { state } = useAuth();


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

    return <>{children}</>;
  }

  // ModeGuard owns loading, error, and anonymous screens.
  return null;
}
