"use client";

import React, { useState } from "react";
import Link from "next/link";
import { platformApi, PlatformApiError } from "@/lib/platform-api-client";
import { CheckCircle2, Loader2, MailCheck, ShieldAlert, UserPlus } from "lucide-react";

export default function RegisterPage() {
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [loading, setLoading] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (loading) return;

    const trimmedEmail = email.trim();
    const trimmedName = displayName.trim();

    if (!trimmedEmail) {
      setError("Email address is required");
      return;
    }
    if (!trimmedName) {
      setError("Display name is required");
      return;
    }

    setLoading(true);
    setError(null);

    try {
      await platformApi.register({
        email: trimmedEmail,
        display_name: trimmedName,
      });
      // 202 Accepted: always show standard accepted message
      setSubmitted(true);
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        if (err.code === "RATE_LIMITED") {
          setError("Too many registration requests. Please try again later.");
        } else {
          setError(err.message || "Registration request failed");
        }
      } else {
        setError(err instanceof Error ? err.message : "An unexpected error occurred");
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-[75vh] flex items-center justify-center p-4">
      <div className="surface max-w-md w-full mx-auto p-6 sm:p-8 space-y-6">
        {submitted ? (
          <div className="text-center space-y-4">
            <div className="inline-flex w-12 h-12 rounded-xl bg-moss-950 border border-moss-800 items-center justify-center text-moss-300">
              <CheckCircle2 className="w-6 h-6" />
            </div>
            <h1 className="text-xl sm:text-2xl font-bold text-white">
              Check your inbox
            </h1>
            <p className="text-xs sm:text-sm text-limestone-300 leading-relaxed">
              If an account can be created for <strong className="text-white">{email}</strong>,
              we have sent a verification link.
            </p>
            <div className="surface border-ink-800 bg-ink-900/60 p-4 text-xs text-limestone-400 text-left space-y-2">
              <div className="flex items-center gap-2 font-medium text-limestone-200">
                <MailCheck className="w-4 h-4 text-copper-400" />
                Next steps:
              </div>
              <ul className="list-disc pl-5 space-y-1">
                <li>Open the email sent to your inbox.</li>
                <li>Click the verification link to choose your password.</li>
                <li>Links expire after 24 hours.</li>
              </ul>
            </div>
            <div className="pt-2">
              <Link href="/login" className="button-primary w-full">
                Back to Sign in
              </Link>
            </div>
          </div>
        ) : (
          <>
            <div className="text-center space-y-2">
              <div className="inline-flex w-12 h-12 rounded-xl bg-copper-950 border border-copper-800 items-center justify-center text-copper-300 mb-2">
                <UserPlus className="w-6 h-6" />
              </div>
              <h1 className="text-xl sm:text-2xl font-bold text-white">
                Create an account
              </h1>
              <p className="text-xs sm:text-sm text-limestone-400">
                Sign up to configure voice agents and manage API access
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

            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label htmlFor="register-email" className="field-label">
                  Email address
                </label>
                <input
                  id="register-email"
                  type="email"
                  required
                  autoComplete="email"
                  autoFocus
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  disabled={loading}
                  placeholder="you@example.com"
                  className="field-control"
                />
              </div>

              <div>
                <label htmlFor="register-name" className="field-label">
                  Display name
                </label>
                <input
                  id="register-name"
                  type="text"
                  required
                  autoComplete="name"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  disabled={loading}
                  placeholder="Your Name"
                  className="field-control"
                />
              </div>

              <button
                type="submit"
                disabled={loading || !email || !displayName}
                className="button-primary w-full mt-2"
              >
                {loading ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin" />
                    Submitting...
                  </>
                ) : (
                  "Continue with Email"
                )}
              </button>
            </form>

            <div className="border-t border-ink-800 pt-4 text-center text-xs text-limestone-400">
              Already have an account?{" "}
              <Link
                href="/login"
                className="text-copper-400 hover:text-copper-300 font-medium transition-colors"
              >
                Sign in
              </Link>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
