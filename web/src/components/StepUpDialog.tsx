"use client";

import React, { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-state";
import { platformApi, PlatformApiError } from "@/lib/platform-api-client";
import { KeyRound, Loader2, X } from "lucide-react";

export function StepUpDialog() {
  const { stepUpManager } = useAuth();
  const [isOpen, setIsOpen] = useState(stepUpManager.isStepUpOpen);
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const unsubscribe = stepUpManager.subscribe(() => {
      setIsOpen(stepUpManager.isStepUpOpen);
      if (!stepUpManager.isStepUpOpen) {
        setPassword("");
        setError(null);
        setLoading(false);
      }
    });
    return () => {
      unsubscribe();
    };
  }, [stepUpManager]);

  if (!isOpen) {
    return null;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (loading) return;

    if (!password) {
      setError("Password is required for step-up authentication");
      return;
    }

    setLoading(true);
    setError(null);

    try {
      await stepUpManager.resolveStepUp(async () => {
        return await platformApi.stepUp({ password });
      });
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        if (err.code === "INVALID_CREDENTIALS") {
          setError("Invalid password");
        } else if (err.code === "RATE_LIMITED") {
          setError("Too many failed attempts. Please try again later.");
        } else {
          setError(err.message || "Step-up authentication failed");
        }
      } else {
        setError(err instanceof Error ? err.message : "Authentication error");
      }
      setLoading(false);
    }
  };

  const handleCancel = () => {
    if (loading) return;
    stepUpManager.cancelStepUp();
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="step-up-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-xs"
    >
      <div className="surface w-full max-w-md p-6 space-y-5 border-copper-700/60 shadow-2xl relative">
        <button
          type="button"
          onClick={handleCancel}
          disabled={loading}
          aria-label="Cancel step-up authentication"
          className="absolute top-4 right-4 text-limestone-400 hover:text-white transition-colors disabled:opacity-50"
        >
          <X className="h-5 w-5" />
        </button>

        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-copper-950 border border-copper-800 flex items-center justify-center text-copper-300">
            <KeyRound className="h-5 w-5" />
          </div>
          <div>
            <h2 id="step-up-title" className="text-base font-semibold text-white">
              Privileged Step-Up Required
            </h2>
            <p className="text-xs text-limestone-400">
              Confirm your admin credentials to proceed with this sensitive action.
            </p>
          </div>
        </div>

        {error && (
          <div
            role="alert"
            className="surface border-rust-800 bg-rust-950/40 p-3 text-xs text-rust-200"
          >
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="step-up-password" className="field-label">
              Admin Password
            </label>
            <input
              id="step-up-password"
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={loading}
              placeholder="Enter current password"
              className="field-control"
            />
          </div>

          <div className="flex items-center justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={handleCancel}
              disabled={loading}
              className="button-secondary"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading || !password}
              className="button-primary min-w-[120px]"
            >
              {loading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Verifying...
                </>
              ) : (
                "Verify & Proceed"
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
