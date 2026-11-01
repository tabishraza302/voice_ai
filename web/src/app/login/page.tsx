"use client";

import React, { useState, useEffect, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { platformApi, PlatformApiError } from "@/lib/platform-api-client";
import { sanitizeReturnPath, useAuth } from "@/lib/auth-state";
import { validatePassword } from "@/types/auth";
import devAccounts from "@/lib/dev-accounts.json";
import { KeyRound, Loader2, LogIn, ShieldAlert } from "lucide-react";

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { refreshSession } = useAuth();

  const nextParam = searchParams.get("next");
  const returnPath = sanitizeReturnPath(nextParam, "/");

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaCode, setMfaCode] = useState("");
  const [isMfaStep, setIsMfaStep] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showDevAccounts, setShowDevAccounts] = useState(false);

  useEffect(() => {
    setShowDevAccounts(process.env.NODE_ENV === "development" && ["localhost", "127.0.0.1", "::1", "[::1]"].includes(window.location.hostname));
  }, []);

  const signIn = async (loginEmail: string, loginPassword: string) => {
    if (loading) return;

    const trimmedEmail = loginEmail.trim();
    if (!trimmedEmail) {
      setError("Please enter your email address");
      return;
    }

    const passwordCheck = validatePassword(loginPassword);
    if (!passwordCheck.valid) {
      setError(passwordCheck.error || "Password does not meet length requirements");
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const response = await platformApi.login({
        email: trimmedEmail,
        password: loginPassword,
      });

      if (response.state === "authenticated") {
        await refreshSession();
        router.push(returnPath);
      } else if (response.state === "mfa_required") {
        setIsMfaStep(true);
        setLoading(false);
      } else if (response.state === "mfa_enrollment_required") {
        await refreshSession();
        router.push("/admin/settings");
      }
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        if (err.code === "INVALID_CREDENTIALS") {
          setError("Invalid email or password");
        } else if (err.code === "RATE_LIMITED") {
          setError("Too many login attempts. Please try again later.");
        } else if (err.code === "ACCOUNT_SUSPENDED") {
          setError("This account has been suspended");
        } else {
          setError(err.message || "Login failed");
        }
      } else {
        setError(err instanceof Error ? err.message : "An unexpected error occurred");
      }
      setLoading(false);
    }
  };

  const handlePasswordLogin = (e: React.FormEvent) => {
    e.preventDefault();
    void signIn(email, password);
  };

  const handleMfaVerify = async (e: React.FormEvent) => {
    e.preventDefault();
    if (loading) return;

    const cleanCode = mfaCode.replace(/\s+/g, "");
    if (!/^\d{6}$/.test(cleanCode)) {
      setError("Please enter a valid 6-digit authenticator code");
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const response = await platformApi.verifyMfa({ code: cleanCode });
      if (response.state === "authenticated") {
        await refreshSession();
        router.push(returnPath);
      }
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        if (err.code === "MFA_INVALID") {
          setError("Invalid or expired verification code");
        } else if (err.code === "RATE_LIMITED") {
          setError("Too many attempts. Please try again later.");
        } else {
          setError(err.message || "MFA verification failed");
        }
      } else {
        setError(err instanceof Error ? err.message : "Verification error");
      }
      setMfaCode("");
      setLoading(false);
    }
  };

  return (
    <div className="surface max-w-md w-full mx-auto p-6 sm:p-8 space-y-6">
      <div className="text-center space-y-2">
        <div className="inline-flex w-12 h-12 rounded-xl bg-copper-950 border border-copper-800 items-center justify-center text-copper-300 mb-2">
          {isMfaStep ? <KeyRound className="w-6 h-6" /> : <LogIn className="w-6 h-6" />}
        </div>
        <h1 className="text-xl sm:text-2xl font-bold text-white">
          {isMfaStep ? "Two-factor verification" : "Sign in to Voice AI"}
        </h1>
        <p className="text-xs sm:text-sm text-limestone-400">
          {isMfaStep
            ? "Enter the 6-digit code from your authenticator app"
            : "Enter your credentials to access your workspace"}
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

      {isMfaStep ? (
        <form onSubmit={handleMfaVerify} className="space-y-4">
          <div>
            <label htmlFor="mfa-code" className="field-label">
              Authenticator Code
            </label>
            <input
              id="mfa-code"
              type="text"
              inputMode="numeric"
              pattern="[0-9]*"
              maxLength={6}
              required
              autoFocus
              autoComplete="one-time-code"
              value={mfaCode}
              onChange={(e) => setMfaCode(e.target.value)}
              disabled={loading}
              placeholder="123456"
              className="field-control text-center tracking-widest font-mono text-lg"
            />
          </div>

          <button
            type="submit"
            disabled={loading || !mfaCode}
            className="button-primary w-full"
          >
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Verifying...
              </>
            ) : (
              "Confirm & Sign in"
            )}
          </button>

          <div className="text-center pt-2">
            <button
              type="button"
              onClick={() => {
                setIsMfaStep(false);
                setMfaCode("");
                setError(null);
              }}
              className="text-xs text-limestone-400 hover:text-white transition-colors"
            >
              Back to password sign in
            </button>
          </div>
        </form>
      ) : (
        <form onSubmit={handlePasswordLogin} className="space-y-4">
          <div>
            <label htmlFor="login-email" className="field-label">
              Email address
            </label>
            <input
              id="login-email"
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
            <div className="flex items-center justify-between mb-1.5">
              <label htmlFor="login-password" className="text-sm font-medium text-limestone-300">
                Password
              </label>
              <Link
                href="/forgot-password"
                className="text-xs text-copper-400 hover:text-copper-300 transition-colors"
              >
                Forgot password?
              </Link>
            </div>
            <input
              id="login-password"
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={loading}
              placeholder="Enter password (15–128 characters)"
              className="field-control"
            />
          </div>

          <button
            type="submit"
            disabled={loading || !email || !password}
            className="button-primary w-full mt-2"
          >
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Signing in...
              </>
            ) : (
              "Sign in"
            )}
          </button>
        </form>
      )}

      {!isMfaStep && showDevAccounts && (
        <section className="border-t border-ink-800 pt-4 space-y-3" aria-label="Local development accounts">
          <h2 className="text-sm font-semibold text-white">Local development accounts</h2>
          <p className="text-xs text-limestone-400">Administrator access requires authenticator setup.</p>
          {devAccounts.map((account) => (
            <div key={account.email} className="rounded-lg border border-ink-800 p-3 space-y-2">
              <p className="text-sm text-white">{account.label}</p>
              <p className="text-xs text-limestone-400 break-all">{account.email} · {account.password}</p>
              <button type="button" disabled={loading} className="button-secondary w-full"
                onClick={() => { setEmail(account.email); setPassword(account.password); void signIn(account.email, account.password); }}>
                Sign in as {account.label}
              </button>
            </div>
          ))}
        </section>
      )}

      {!isMfaStep && (
        <div className="border-t border-ink-800 pt-4 text-center text-xs text-limestone-400">
          Don&apos;t have an account?{" "}
          <Link
            href="/register"
            className="text-copper-400 hover:text-copper-300 font-medium transition-colors"
          >
            Create account
          </Link>
        </div>
      )}
    </div>
  );
}

export default function LoginPage() {
  return (
    <div className="min-h-[75vh] flex items-center justify-center p-4">
      <Suspense
        fallback={
          <div className="surface max-w-md w-full mx-auto p-8 text-center text-limestone-400">
            <Loader2 className="w-6 h-6 animate-spin mx-auto text-copper-400 mb-2" />
            Loading sign in...
          </div>
        }
      >
        <LoginForm />
      </Suspense>
    </div>
  );
}
