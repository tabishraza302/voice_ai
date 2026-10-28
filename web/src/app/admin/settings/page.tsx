"use client";

import React, { useState } from "react";
import { AdminGate } from "@/components/AdminGate";
import { useAuth } from "@/lib/auth-state";
import { platformApi, PlatformApiError } from "@/lib/platform-api-client";
import type { MfaSetup } from "@/types/auth";
import {
  CheckCircle2,
  Clock,
  Copy,
  KeyRound,
  Loader2,
  QrCode,
  ShieldAlert,
  ShieldCheck,
  Smartphone,
} from "lucide-react";

function AdminSettingsContent() {
  const { state, refreshSession, executeWithStepUp } = useAuth();
  const session = state.session;
  const user = state.user;

  // Enrollment state
  const [currentPassword, setCurrentPassword] = useState("");
  const [enrollLoading, setEnrollLoading] = useState(false);
  const [setupData, setSetupData] = useState<MfaSetup | null>(null);
  const [confirmCode, setConfirmCode] = useState("");
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [copiedSecret, setCopiedSecret] = useState(false);
  const [mfaError, setMfaError] = useState<string | null>(null);
  const [mfaSuccess, setMfaSuccess] = useState<string | null>(null);

  // Manual Step-up state
  const [stepUpPassword, setStepUpPassword] = useState("");
  const [stepUpCode, setStepUpCode] = useState("");
  const [stepUpLoading, setStepUpLoading] = useState(false);
  const [stepUpError, setStepUpError] = useState<string | null>(null);
  const [stepUpSuccess, setStepUpSuccess] = useState<string | null>(null);

  const isMfaEnabled = user?.mfa_enabled ?? false;

  const handleStartEnrollment = async (e: React.FormEvent) => {
    e.preventDefault();
    if (enrollLoading || !currentPassword) return;

    setEnrollLoading(true);
    setMfaError(null);
    setMfaSuccess(null);

    try {
      const setup = await platformApi.enrollMfa({
        current_password: currentPassword,
      });
      setSetupData(setup);
      setCurrentPassword("");
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        if (err.code === "INVALID_CREDENTIALS") {
          setMfaError("Incorrect admin password");
        } else if (err.code === "MFA_STATE_CONFLICT") {
          setMfaError("MFA is already configured for this administrator");
        } else {
          setMfaError(err.message || "Failed to initiate MFA setup");
        }
      } else {
        setMfaError(err instanceof Error ? err.message : "Enrollment error");
      }
    } finally {
      setEnrollLoading(false);
    }
  };

  const handleConfirmMfa = async (e: React.FormEvent) => {
    e.preventDefault();
    if (confirmLoading || !confirmCode) return;

    const cleanCode = confirmCode.replace(/\s+/g, "");
    if (!/^\d{6}$/.test(cleanCode)) {
      setMfaError("Please enter a 6-digit verification code");
      return;
    }

    setConfirmLoading(true);
    setMfaError(null);

    try {
      await platformApi.confirmMfa({ code: cleanCode });
      setSetupData(null);
      setConfirmCode("");
      setMfaSuccess("MFA has been successfully verified and activated on your account.");
      await refreshSession();
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        if (err.code === "MFA_INVALID") {
          setMfaError("Invalid or expired 6-digit code. Please verify the time on your authenticator device.");
        } else {
          setMfaError(err.message || "Confirmation failed");
        }
      } else {
        setMfaError(err instanceof Error ? err.message : "Confirmation error");
      }
      setConfirmCode("");
    } finally {
      setConfirmLoading(false);
    }
  };

  const handleCopySecret = async () => {
    if (!setupData?.secret) return;
    try {
      await navigator.clipboard.writeText(setupData.secret);
      setCopiedSecret(true);
      setTimeout(() => setCopiedSecret(false), 2000);
    } catch {
      // Ignore clipboard write failure
    }
  };

  const handleManualStepUp = async (e: React.FormEvent) => {
    e.preventDefault();
    if (stepUpLoading || !stepUpPassword || !stepUpCode) return;

    const cleanCode = stepUpCode.replace(/\s+/g, "");
    if (!/^\d{6}$/.test(cleanCode)) {
      setStepUpError("Please enter a 6-digit code");
      return;
    }

    setStepUpLoading(true);
    setStepUpError(null);
    setStepUpSuccess(null);

    try {
      await platformApi.stepUp({
        password: stepUpPassword,
        code: cleanCode,
      });
      setStepUpPassword("");
      setStepUpCode("");
      setStepUpSuccess("Step-up authentication confirmed. Privileged mutations are unlocked for 5 minutes.");
      await refreshSession();
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        if (err.code === "INVALID_CREDENTIALS") {
          setStepUpError("Invalid admin password or authenticator code");
        } else {
          setStepUpError(err.message || "Step-up authentication failed");
        }
      } else {
        setStepUpError(err instanceof Error ? err.message : "Step-up error");
      }
      setStepUpCode("");
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
          Configure two-factor TOTP authentication and privileged step-up authorizations
        </p>
      </header>

      {/* MFA Enrollment / Status Section */}
      <section aria-labelledby="mfa-section-heading" className="surface p-6 space-y-5">
        <div className="flex items-center justify-between border-b border-ink-800 pb-4">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-copper-950 border border-copper-800 flex items-center justify-center text-copper-300">
              <Smartphone className="w-5 h-5" />
            </div>
            <div>
              <h2 id="mfa-section-heading" className="text-base font-semibold text-white">
                Multi-Factor Authentication (TOTP)
              </h2>
              <p className="text-xs text-limestone-400">
                Time-based one-time password security for platform administrator operations
              </p>
            </div>
          </div>
          <div>
            {isMfaEnabled ? (
              <span className="px-2.5 py-1 rounded text-xs font-semibold bg-moss-950 text-moss-300 border border-moss-800 flex items-center gap-1.5">
                <ShieldCheck className="w-3.5 h-3.5" />
                Active
              </span>
            ) : (
              <span className="px-2.5 py-1 rounded text-xs font-semibold bg-rust-950 text-rust-300 border border-rust-800 flex items-center gap-1.5">
                <ShieldAlert className="w-3.5 h-3.5" />
                Required
              </span>
            )}
          </div>
        </div>

        {mfaError && (
          <div
            role="alert"
            className="surface border-rust-800 bg-rust-950/40 p-3.5 text-xs sm:text-sm text-rust-200"
          >
            {mfaError}
          </div>
        )}

        {mfaSuccess && (
          <div
            role="status"
            className="surface border-moss-800 bg-moss-950/40 p-3.5 text-xs sm:text-sm text-moss-200 flex items-center gap-2"
          >
            <CheckCircle2 className="w-4 h-4 text-moss-400 shrink-0" />
            <span>{mfaSuccess}</span>
          </div>
        )}

        {isMfaEnabled ? (
          <div className="surface p-4 bg-ink-900 border-ink-800 flex items-start gap-3">
            <ShieldCheck className="w-5 h-5 text-moss-400 shrink-0 mt-0.5" />
            <div className="text-xs sm:text-sm text-limestone-300 space-y-1">
              <p className="font-medium text-white">
                Your administrator account is protected with MFA.
              </p>
              <p className="text-limestone-400">
                A 6-digit TOTP code is required on every administrator login and before executing sensitive mutations. To replace your factor, an operator can execute the <code className="text-copper-300 bg-ink-950 px-1 py-0.5 rounded font-mono text-[11px]">recover-admin-mfa</code> CLI command.
              </p>
            </div>
          </div>
        ) : setupData ? (
          /* Step 2 of setup: Provisioning secret and code entry */
          <div className="space-y-5 border border-copper-800/60 rounded-xl p-5 bg-copper-950/10">
            <div className="space-y-1">
              <h3 className="text-sm font-semibold text-white">
                Step 2: Add to your authenticator app
              </h3>
              <p className="text-xs text-limestone-400">
                Scan the provisioning URI or copy the secret key into Google Authenticator, 1Password, or an offline TOTP app.
              </p>
            </div>

            <div className="space-y-3">
              <div>
                <label htmlFor="totp-secret-key" className="field-label text-xs">
                  TOTP Secret Key (Base32)
                </label>
                <div className="flex gap-2">
                  <input
                    id="totp-secret-key"
                    type="text"
                    readOnly
                    value={setupData.secret}
                    className="field-control font-mono tracking-wider text-xs select-all bg-ink-900"
                  />
                  <button
                    type="button"
                    onClick={handleCopySecret}
                    className="button-secondary text-xs px-3 inline-flex items-center gap-1.5 shrink-0"
                  >
                    {copiedSecret ? (
                      <>
                        <CheckCircle2 className="w-3.5 h-3.5 text-moss-400" />
                        Copied
                      </>
                    ) : (
                      <>
                        <Copy className="w-3.5 h-3.5" />
                        Copy
                      </>
                    )}
                  </button>
                </div>
              </div>

              <div>
                <label htmlFor="totp-provisioning-uri" className="field-label text-xs">
                  Provisioning URI
                </label>
                <input
                  id="totp-provisioning-uri"
                  type="text"
                  readOnly
                  value={setupData.provisioning_uri}
                  className="field-control font-mono text-[11px] select-all bg-ink-900 text-limestone-400"
                />
              </div>
            </div>

            <form onSubmit={handleConfirmMfa} className="space-y-4 pt-2 border-t border-ink-800">
              <div className="space-y-1">
                <label htmlFor="confirm-totp" className="field-label">
                  Step 3: Enter the generated 6-digit code
                </label>
                <input
                  id="confirm-totp"
                  type="text"
                  inputMode="numeric"
                  pattern="[0-9]*"
                  maxLength={6}
                  required
                  autoFocus
                  autoComplete="one-time-code"
                  value={confirmCode}
                  onChange={(e) => setConfirmCode(e.target.value)}
                  disabled={confirmLoading}
                  placeholder="123456"
                  className="field-control text-center tracking-widest font-mono text-base max-w-xs"
                />
              </div>

              <div className="flex items-center gap-3">
                <button
                  type="submit"
                  disabled={confirmLoading || !confirmCode}
                  className="button-primary min-w-[140px]"
                >
                  {confirmLoading ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" />
                      Verifying...
                    </>
                  ) : (
                    "Confirm & Enable MFA"
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => setSetupData(null)}
                  disabled={confirmLoading}
                  className="button-secondary text-xs"
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        ) : (
          /* Step 1 of setup: Prompt for password */
          <form onSubmit={handleStartEnrollment} className="space-y-4">
            <p className="text-xs sm:text-sm text-limestone-300">
              To enroll a new authenticator factor, please confirm your current admin password.
            </p>
            <div className="max-w-md">
              <label htmlFor="enroll-pw" className="field-label">
                Current admin password
              </label>
              <input
                id="enroll-pw"
                type="password"
                required
                autoComplete="current-password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                disabled={enrollLoading}
                placeholder="Enter password"
                className="field-control"
              />
            </div>
            <button
              type="submit"
              disabled={enrollLoading || !currentPassword}
              className="button-primary inline-flex items-center gap-2"
            >
              {enrollLoading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Generating key...
                </>
              ) : (
                <>
                  <QrCode className="w-4 h-4" />
                  Begin MFA Enrollment
                </>
              )}
            </button>
          </form>
        )}
      </section>

      {/* Privileged Step-Up Status and Refresh */}
      {isMfaEnabled && (
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
                . You can perform privileged management mutations without repeating your TOTP code.
              </p>
            ) : (
              <p>
                Privileged step-up is currently inactive. You can verify your credentials now to unlock administrative actions for the next 5 minutes.
              </p>
            )}
          </div>

          <form onSubmit={handleManualStepUp} className="space-y-4 pt-2">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 max-w-md">
              <div>
                <label htmlFor="manual-stepup-pw" className="field-label">
                  Password
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
              <div>
                <label htmlFor="manual-stepup-code" className="field-label">
                  6-Digit TOTP Code
                </label>
                <input
                  id="manual-stepup-code"
                  type="text"
                  inputMode="numeric"
                  pattern="[0-9]*"
                  maxLength={6}
                  required
                  autoComplete="one-time-code"
                  value={stepUpCode}
                  onChange={(e) => setStepUpCode(e.target.value)}
                  disabled={stepUpLoading}
                  placeholder="123456"
                  className="field-control text-center tracking-widest font-mono"
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={stepUpLoading || !stepUpPassword || !stepUpCode}
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
      )}
    </div>
  );
}

export default function AdminSettingsPage() {
  return (
    <AdminGate>
      <AdminSettingsContent />
    </AdminGate>
  );
}
