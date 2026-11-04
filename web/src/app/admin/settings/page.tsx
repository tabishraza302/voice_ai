"use client";

import React, { useState } from "react";
import { useAuth } from "@/lib/auth-state";
import { platformApi, PlatformApiError } from "@/lib/platform-api-client";
import {
  CheckCircle2,
  Clock,
  KeyRound,
  Loader2,
} from "lucide-react";

function AdminSettingsContent() {
  const { state, refreshSession } = useAuth();
  const session = state.session;


  // Manual Step-up state
  const [stepUpPassword, setStepUpPassword] = useState("");
  const [stepUpLoading, setStepUpLoading] = useState(false);
  const [stepUpError, setStepUpError] = useState<string | null>(null);
  const [stepUpSuccess, setStepUpSuccess] = useState<string | null>(null);



  const handleManualStepUp = async (e: React.FormEvent) => {
    e.preventDefault();
    if (stepUpLoading || !stepUpPassword) return;

    setStepUpLoading(true);
    setStepUpError(null);
    setStepUpSuccess(null);

    try {
      await platformApi.stepUp({ password: stepUpPassword });
      setStepUpPassword("");
      setStepUpSuccess("Password confirmed. Privileged mutations are unlocked for 5 minutes.");
      await refreshSession();
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        if (err.code === "INVALID_CREDENTIALS") {
          setStepUpError("Incorrect admin password");
        } else if (err.code === "RATE_LIMITED") {
          setStepUpError("Too many failed attempts. Please try again later.");
        } else {
          setStepUpError(err.message || "Step-up authentication failed");
        }
      } else {
        setStepUpError(err instanceof Error ? err.message : "Step-up error");
      }
    } finally {
      setStepUpLoading(false);
    }
  };

  const stepUpExpiresAt = session?.step_up_expires_at
    ? new Date(session.step_up_expires_at)
    : null;
  const isStepUpActive =
    stepUpExpiresAt !== null && stepUpExpiresAt.getTime() > Date.now();

  return (
    <div className="space-y-8 max-w-4xl">
      <header className="border-b border-ink-800 pb-5">
        <h1 className="text-2xl font-bold text-white">Administrator Security</h1>
        <p className="text-sm text-limestone-400 mt-1">
          Confirm your password here or when prompted before privileged actions
        </p>
      </header>


      <section aria-labelledby="step-up-heading" className="surface p-6 space-y-5">
          <div className="flex items-center justify-between border-b border-ink-800 pb-4">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-lg bg-copper-950 border border-copper-800 flex items-center justify-center text-copper-300">
                <KeyRound className="w-5 h-5" />
              </div>
              <div>
                <h2 id="step-up-heading" className="text-base font-semibold text-white">
                  Step-Up Authentication Status
                </h2>
                <p className="text-xs text-limestone-400">
                  Sensitive actions (such as account suspensions or global policy edits) require a fresh step-up within 5 minutes
                </p>
              </div>
            </div>
            <div>
              {isStepUpActive ? (
                <span className="px-2.5 py-1 rounded text-xs font-semibold bg-moss-950 text-moss-300 border border-moss-800 flex items-center gap-1.5">
                  <Clock className="w-3.5 h-3.5" />
                  Active
                </span>
              ) : (
                <span className="px-2.5 py-1 rounded text-xs font-semibold bg-ink-800 text-limestone-400 border border-ink-700 flex items-center gap-1.5">
                  <Clock className="w-3.5 h-3.5" />
                  Expired / Inactive
                </span>
              )}
            </div>
          </div>

          {stepUpError && (
            <div
              role="alert"
              className="surface border-rust-800 bg-rust-950/40 p-3 text-xs text-rust-200"
            >
              {stepUpError}
            </div>
          )}

          {stepUpSuccess && (
            <div
              role="status"
              className="surface border-moss-800 bg-moss-950/40 p-3 text-xs text-moss-200 flex items-center gap-2"
            >
              <CheckCircle2 className="w-4 h-4 text-moss-400 shrink-0" />
              <span>{stepUpSuccess}</span>
            </div>
          )}

          <div className="text-xs sm:text-sm text-limestone-300">
            {isStepUpActive ? (
              <p>
                Step-up authorization is active until{" "}
                <strong className="text-white">
                  {stepUpExpiresAt?.toLocaleTimeString()}
                </strong>
                . You can perform privileged management mutations without repeating your password.
              </p>
            ) : (
              <p>
                Privileged step-up is currently inactive. You can verify your password now to unlock administrative actions for the next 5 minutes.
              </p>
            )}
          </div>

          <form onSubmit={handleManualStepUp} className="space-y-4 pt-2">
            <div className="max-w-md">
              <label htmlFor="manual-stepup-pw" className="field-label">
                Admin password
              </label>
              <input
                id="manual-stepup-pw"
                type="password"
                required
                autoComplete="current-password"
                value={stepUpPassword}
                onChange={(e) => setStepUpPassword(e.target.value)}
                disabled={stepUpLoading}
                className="field-control"
              />
            </div>

            <button
              type="submit"
              disabled={stepUpLoading || !stepUpPassword}
              className="button-primary inline-flex items-center gap-2"
            >
              {stepUpLoading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Verifying...
                </>
              ) : (
                "Refresh Step-Up Authorization"
              )}
            </button>
          </form>
        </section>
    </div>
  );
}

export default function AdminSettingsPage() {
  return <AdminSettingsContent />;
}
