"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { extractAndClearFragmentToken } from "@/lib/auth-state";
import { platformApi, PlatformApiError } from "@/lib/platform-api-client";
import { validatePassword } from "@/types/auth";
import { CheckCircle2, KeyRound, Loader2, ShieldAlert } from "lucide-react";

export default function ResetPasswordPage() {
  const [token, setToken] = useState<string | null>(null);
  const [checkedFragment, setCheckedFragment] = useState(false);
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [resetSuccess, setResetSuccess] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Read token from URL fragment into memory and immediately remove fragment from history/URL
    const fragmentToken = extractAndClearFragmentToken();
    if (fragmentToken) {
      setToken(fragmentToken);
    }
    setCheckedFragment(true);
  }, []);

  const handleReset = async (e: React.FormEvent) => {
    e.preventDefault();
    if (loading || !token) return;

    if (password !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }

    const check = validatePassword(password);
    if (!check.valid) {
      setError(check.error || "Password does not meet requirements");
      return;
    }

    setLoading(true);
    setError(null);

    try {
      await platformApi.confirmPasswordReset({
        token,
        password,
      });
      // 204 No Content
      setResetSuccess(true);
      setToken(null);
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        if (err.code === "TOKEN_INVALID") {
          setError(
            "This password reset link is invalid, expired, or has already been used. Please request a new link."
          );
        } else if (err.code === "RATE_LIMITED") {
          setError("Too many attempts. Please try again later.");
        } else {
          setError(err.message || "Password reset failed");
        }
      } else {
        setError(err instanceof Error ? err.message : "An unexpected error occurred");
      }
    } finally {
      setLoading(false);
    }
  };

  if (!checkedFragment) {
    return (
      <div className="min-h-[75vh] flex items-center justify-center p-4">
        <div className="surface max-w-md w-full mx-auto p-8 text-center text-limestone-400">
          <Loader2 className="w-6 h-6 animate-spin mx-auto text-copper-400 mb-2" />
          Checking reset link...
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-[75vh] flex items-center justify-center p-4">
      <div className="surface max-w-md w-full mx-auto p-6 sm:p-8 space-y-6">
        {resetSuccess ? (
          <div className="text-center space-y-4">
            <div className="inline-flex w-12 h-12 rounded-xl bg-moss-950 border border-moss-800 items-center justify-center text-moss-300">
              <CheckCircle2 className="w-6 h-6" />
            </div>
            <h1 className="text-xl sm:text-2xl font-bold text-white">
              Password updated
            </h1>
            <p className="text-xs sm:text-sm text-limestone-300 leading-relaxed">
              Your password has been successfully reset. All previous sessions have been signed out. You can now sign in with your new password.
            </p>
            <div className="pt-2">
              <Link href="/login" className="button-primary w-full">
                Sign in with new password
              </Link>
            </div>
          </div>
        ) : !token ? (
          <div className="text-center space-y-4">
            <div className="inline-flex w-12 h-12 rounded-xl bg-rust-950 border border-rust-800 items-center justify-center text-rust-300">
              <ShieldAlert className="w-6 h-6" />
            </div>
            <h1 className="text-xl sm:text-2xl font-bold text-white">
              Invalid or missing link
            </h1>
            <p className="text-xs sm:text-sm text-limestone-400 leading-relaxed">
              No reset token was found in this link. Reset links expire after 30 minutes and can only be used once.
            </p>
            <div className="pt-2 flex flex-col gap-2">
              <Link href="/forgot-password" className="button-primary w-full">
                Request new reset link
              </Link>
              <Link href="/login" className="button-secondary w-full">
                Back to Sign in
              </Link>
            </div>
          </div>
        ) : (
          <>
            <div className="text-center space-y-2">
              <div className="inline-flex w-12 h-12 rounded-xl bg-copper-950 border border-copper-800 items-center justify-center text-copper-300 mb-2">
                <KeyRound className="w-6 h-6" />
              </div>
              <h1 className="text-xl sm:text-2xl font-bold text-white">
                Choose a new password
              </h1>
              <p className="text-xs sm:text-sm text-limestone-400">
                Enter your new password (15–128 characters)
              </p>
            </div>

            {error && (
              <div
                role="alert"
                className="surface flex items-start gap-3 border-rust-800 bg-rust-950/40 p-3.5 text-xs sm:text-sm text-rust-200"
              >
                <ShieldAlert className="w-5 h-5 text-rust-400 shrink-0 mt-0.5" />
                <span>{error}</span>
              </div>
            )}

            <form onSubmit={handleReset} className="space-y-4">
              <div>
                <label htmlFor="reset-password" className="field-label">
                  New password
                </label>
                <input
                  id="reset-password"
                  type="password"
                  required
                  autoFocus
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={loading}
                  placeholder="At least 15 characters"
                  className="field-control"
                />
              </div>

              <div>
                <label htmlFor="reset-confirm" className="field-label">
                  Confirm new password
                </label>
                <input
                  id="reset-confirm"
                  type="password"
                  required
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  disabled={loading}
                  placeholder="Re-enter your new password"
                  className="field-control"
                />
              </div>

              <button
                type="submit"
                disabled={loading || !password || !confirmPassword}
                className="button-primary w-full mt-2"
              >
                {loading ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin" />
                    Updating password...
                  </>
                ) : (
                  "Reset Password"
                )}
              </button>
            </form>
          </>
        )}
      </div>
    </div>
  );
}
