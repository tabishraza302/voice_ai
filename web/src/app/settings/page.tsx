"use client";

import React, { useEffect, useState } from "react";
import { AuthGate } from "@/components/AuthGate";
import { useAuth } from "@/lib/auth-state";
import { platformApi, PlatformApiError } from "@/lib/platform-api-client";
import { validatePassword, type SessionItem, type UserView } from "@/types/auth";
import {
  Check,
  Globe,
  KeyRound,
  Laptop,
  Loader2,
  LogOut,
  Save,
  Shield,
  ShieldAlert,
  Smartphone,
  Trash2,
  User,
} from "lucide-react";
import { useRouter } from "next/navigation";

function SettingsContent() {
  const { state, isLocalMode, logout, refreshSession } = useAuth();
  const router = useRouter();

  // Profile state
  const [profile, setProfile] = useState<UserView | null>(state.user);
  const [displayName, setDisplayName] = useState(state.user?.display_name || "");
  const [language, setLanguage] = useState<"en" | "hi">(
    state.user?.preferred_language || "en"
  );
  const [timezone, setTimezone] = useState(
    state.user?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"
  );
  const [lowBalanceNotifications, setLowBalanceNotifications] = useState(
    state.user?.low_balance_notifications ?? true
  );
  const [profileSaving, setProfileSaving] = useState(false);
  const [profileMessage, setProfileMessage] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);

  // Password state
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordSaving, setPasswordSaving] = useState(false);
  const [passwordMessage, setPasswordMessage] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);

  // Sessions state
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionActionLoading, setSessionActionLoading] = useState<string | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);

  useEffect(() => {
    if (state.user) {
      setProfile(state.user);
      setDisplayName(state.user.display_name);
      setLanguage(state.user.preferred_language);
      setTimezone(state.user.timezone);
      setLowBalanceNotifications(state.user.low_balance_notifications);
    }
  }, [state.user]);

  const loadSessions = async () => {
    if (isLocalMode) return;
    setSessionsLoading(true);
    setSessionError(null);
    try {
      const result = await platformApi.getSessions();
      setSessions(result.items);
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        setSessionError(err.message);
      } else {
        setSessionError("Failed to load active sessions");
      }
    } finally {
      setSessionsLoading(false);
    }
  };

  useEffect(() => {
    if (!isLocalMode) {
      loadSessions();
    }
  }, [isLocalMode]);

  const handleUpdateProfile = async (e: React.FormEvent) => {
    e.preventDefault();
    if (profileSaving) return;

    if (isLocalMode) {
      setProfileMessage({
        type: "success",
        text: "Profile settings saved locally",
      });
      return;
    }

    const trimmedName = displayName.trim();
    if (!trimmedName) {
      setProfileMessage({ type: "error", text: "Display name cannot be empty" });
      return;
    }

    setProfileSaving(true);
    setProfileMessage(null);

    try {
      const updated = await platformApi.updateMe({
        display_name: trimmedName,
        preferred_language: language,
        timezone,
        low_balance_notifications: lowBalanceNotifications,
      });
      setProfile(updated);
      await refreshSession();
      setProfileMessage({ type: "success", text: "Profile updated successfully" });
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        setProfileMessage({
          type: "error",
          text: err.message || "Failed to update profile",
        });
      } else {
        setProfileMessage({ type: "error", text: "An unexpected error occurred" });
      }
    } finally {
      setProfileSaving(false);
    }
  };

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    if (passwordSaving) return;

    if (isLocalMode) {
      setPasswordMessage({
        type: "error",
        text: "Password management is disabled in local offline mode",
      });
      return;
    }

    if (newPassword !== confirmPassword) {
      setPasswordMessage({ type: "error", text: "New passwords do not match" });
      return;
    }

    const check = validatePassword(newPassword);
    if (!check.valid) {
      setPasswordMessage({
        type: "error",
        text: check.error || "Password does not meet requirements (15–128 characters)",
      });
      return;
    }

    setPasswordSaving(true);
    setPasswordMessage(null);

    try {
      await platformApi.changePassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPasswordMessage({
        type: "success",
        text: "Password changed successfully. Other sessions have been signed out.",
      });
      loadSessions();
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        if (err.code === "INVALID_CREDENTIALS") {
          setPasswordMessage({ type: "error", text: "Current password is incorrect" });
        } else if (err.code === "VALIDATION") {
          setPasswordMessage({
            type: "error",
            text: err.message || "New password does not satisfy validation rules",
          });
        } else {
          setPasswordMessage({
            type: "error",
            text: err.message || "Password change failed",
          });
        }
      } else {
        setPasswordMessage({ type: "error", text: "An unexpected error occurred" });
      }
    } finally {
      setPasswordSaving(false);
    }
  };

  const handleRevokeSession = async (sessionId: string) => {
    setSessionActionLoading(sessionId);
    try {
      await platformApi.revokeSession(sessionId);
      await loadSessions();
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        setSessionError(err.message);
      }
    } finally {
      setSessionActionLoading(null);
    }
  };

  const handleLogoutAll = async () => {
    setSessionActionLoading("all");
    try {
      await platformApi.logoutAll();
      router.push("/login");
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        setSessionError(err.message);
      }
    } finally {
      setSessionActionLoading(null);
    }
  };

  const handleLogout = async () => {
    await logout();
    router.push("/login");
  };

  return (
    <div className="space-y-8 max-w-4xl">
      <header className="border-b border-ink-800 pb-5">
        <h1 className="text-2xl font-bold text-white">Account Settings</h1>
        <p className="text-sm text-limestone-400 mt-1">
          Manage your personal profile, security preferences, and active browser sessions
        </p>
      </header>

      {/* Profile Section */}
      <section aria-labelledby="profile-heading" className="surface p-6 space-y-5">
        <div className="flex items-center gap-3 border-b border-ink-800 pb-4">
          <div className="w-9 h-9 rounded-lg bg-copper-950 border border-copper-800 flex items-center justify-center text-copper-300">
            <User className="w-5 h-5" />
          </div>
          <div>
            <h2 id="profile-heading" className="text-base font-semibold text-white">
              Profile details
            </h2>
            <p className="text-xs text-limestone-400">
              Identity information and language preferences
            </p>
          </div>
        </div>

        {profileMessage && (
          <div
            role="alert"
            className={`surface p-3.5 text-xs sm:text-sm flex items-center gap-2 ${
              profileMessage.type === "success"
                ? "border-moss-800 bg-moss-950/40 text-moss-200"
                : "border-rust-800 bg-rust-950/40 text-rust-200"
            }`}
          >
            {profileMessage.type === "success" ? (
              <Check className="w-4 h-4 text-moss-400 shrink-0" />
            ) : (
              <ShieldAlert className="w-4 h-4 text-rust-400 shrink-0" />
            )}
            <span>{profileMessage.text}</span>
          </div>
        )}

        <form onSubmit={handleUpdateProfile} className="space-y-4">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label htmlFor="settings-email" className="field-label">
                Email address (Read-only)
              </label>
              <input
                id="settings-email"
                type="email"
                disabled
                value={profile?.email || ""}
                className="field-control opacity-70 cursor-not-allowed bg-ink-900"
              />
            </div>

            <div>
              <label htmlFor="settings-name" className="field-label">
                Display name
              </label>
              <input
                id="settings-name"
                type="text"
                required
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                disabled={profileSaving}
                className="field-control"
              />
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label htmlFor="settings-lang" className="field-label">
                Preferred language
              </label>
              <select
                id="settings-lang"
                value={language}
                onChange={(e) => setLanguage(e.target.value as "en" | "hi")}
                disabled={profileSaving}
                className="field-control"
              >
                <option value="en">English (en)</option>
                <option value="hi">हिन्दी / Hindi (hi)</option>
              </select>
            </div>

            <div>
              <label htmlFor="settings-tz" className="field-label">
                Timezone
              </label>
              <input
                id="settings-tz"
                type="text"
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
                disabled={profileSaving}
                className="field-control"
              />
            </div>
          </div>

          <div className="pt-2">
            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={lowBalanceNotifications}
                onChange={(e) => setLowBalanceNotifications(e.target.checked)}
                disabled={profileSaving}
                className="w-4 h-4 rounded border-ink-700 text-copper-500 focus:ring-copper-400 bg-ink-900"
              />
              <span className="text-xs sm:text-sm text-limestone-300">
                Receive notifications when wallet balance is low
              </span>
            </label>
          </div>

          <div className="pt-2">
            <button
              type="submit"
              disabled={profileSaving}
              className="button-primary inline-flex items-center gap-2"
            >
              {profileSaving ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Saving...
                </>
              ) : (
                <>
                  <Save className="w-4 h-4" />
                  Save Changes
                </>
              )}
            </button>
          </div>
        </form>
      </section>

      {/* Password Management */}
      <section aria-labelledby="password-heading" className="surface p-6 space-y-5">
        <div className="flex items-center gap-3 border-b border-ink-800 pb-4">
          <div className="w-9 h-9 rounded-lg bg-copper-950 border border-copper-800 flex items-center justify-center text-copper-300">
            <KeyRound className="w-5 h-5" />
          </div>
          <div>
            <h2 id="password-heading" className="text-base font-semibold text-white">
              Change password
            </h2>
            <p className="text-xs text-limestone-400">
              Update your account password (15–128 characters)
            </p>
          </div>
        </div>

        {passwordMessage && (
          <div
            role="alert"
            className={`surface p-3.5 text-xs sm:text-sm flex items-center gap-2 ${
              passwordMessage.type === "success"
                ? "border-moss-800 bg-moss-950/40 text-moss-200"
                : "border-rust-800 bg-rust-950/40 text-rust-200"
            }`}
          >
            {passwordMessage.type === "success" ? (
              <Check className="w-4 h-4 text-moss-400 shrink-0" />
            ) : (
              <ShieldAlert className="w-4 h-4 text-rust-400 shrink-0" />
            )}
            <span>{passwordMessage.text}</span>
          </div>
        )}

        <form onSubmit={handleChangePassword} className="space-y-4">
          <div>
            <label htmlFor="current-pw" className="field-label">
              Current password
            </label>
            <input
              id="current-pw"
              type="password"
              required
              autoComplete="current-password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              disabled={passwordSaving}
              className="field-control max-w-md"
            />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label htmlFor="new-pw" className="field-label">
                New password
              </label>
              <input
                id="new-pw"
                type="password"
                required
                autoComplete="new-password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                disabled={passwordSaving}
                placeholder="15–128 characters"
                className="field-control"
              />
            </div>

            <div>
              <label htmlFor="confirm-pw" className="field-label">
                Confirm new password
              </label>
              <input
                id="confirm-pw"
                type="password"
                required
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                disabled={passwordSaving}
                className="field-control"
              />
            </div>
          </div>

          <div className="pt-2">
            <button
              type="submit"
              disabled={passwordSaving || !currentPassword || !newPassword || !confirmPassword}
              className="button-primary inline-flex items-center gap-2"
            >
              {passwordSaving ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Updating password...
                </>
              ) : (
                "Update Password"
              )}
            </button>
          </div>
        </form>
      </section>

      {/* Active Sessions */}
      <section aria-labelledby="sessions-heading" className="surface p-6 space-y-5">
        <div className="flex items-center justify-between border-b border-ink-800 pb-4">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-copper-950 border border-copper-800 flex items-center justify-center text-copper-300">
              <Shield className="w-5 h-5" />
            </div>
            <div>
              <h2 id="sessions-heading" className="text-base font-semibold text-white">
                Active sessions
              </h2>
              <p className="text-xs text-limestone-400">
                Manage signed-in devices and browser instances
              </p>
            </div>
          </div>

          {!isLocalMode && sessions.length > 1 && (
            <button
              type="button"
              onClick={handleLogoutAll}
              disabled={sessionActionLoading === "all"}
              className="button-secondary text-xs"
            >
              {sessionActionLoading === "all" ? "Signing out all..." : "Sign out all devices"}
            </button>
          )}
        </div>

        {sessionError && (
          <div
            role="alert"
            className="surface border-rust-800 bg-rust-950/40 p-3 text-xs text-rust-200"
          >
            {sessionError}
          </div>
        )}

        {isLocalMode ? (
          <div className="text-xs text-limestone-400 p-4 border border-ink-800 rounded-lg">
            Running in local offline mode. Browser sessions are managed locally on this machine.
          </div>
        ) : sessionsLoading ? (
          <div className="flex items-center justify-center p-6 text-limestone-400 text-xs">
            <Loader2 className="w-5 h-5 animate-spin mr-2 text-copper-400" />
            Loading sessions...
          </div>
        ) : (
          <div className="space-y-3">
            {sessions.map((item) => (
              <div
                key={item.id}
                className="surface flex items-center justify-between p-3.5 border-ink-800 bg-ink-900/60"
              >
                <div className="flex items-center gap-3">
                  <div className="w-8 h-8 rounded-lg bg-ink-800 flex items-center justify-center text-limestone-300">
                    {item.user_agent_label.toLowerCase().includes("mobile") ? (
                      <Smartphone className="w-4 h-4" />
                    ) : (
                      <Laptop className="w-4 h-4" />
                    )}
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="text-xs sm:text-sm font-medium text-white">
                        {item.user_agent_label || "Browser session"}
                      </span>
                      {item.current && (
                        <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-moss-950 text-moss-300 border border-moss-800">
                          Current device
                        </span>
                      )}
                    </div>
                    <div className="text-[11px] text-limestone-500">
                      Last seen: {new Date(item.last_seen_at).toLocaleString()} · Expires:{" "}
                      {new Date(item.expires_at).toLocaleDateString()}
                    </div>
                  </div>
                </div>

                {!item.current && (
                  <button
                    type="button"
                    onClick={() => handleRevokeSession(item.id)}
                    disabled={sessionActionLoading === item.id}
                    className="button-secondary text-xs px-2.5 py-1 text-rust-300 hover:text-rust-200 hover:border-rust-800"
                    title="Revoke session"
                  >
                    {sessionActionLoading === item.id ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Trash2 className="w-3.5 h-3.5" />
                    )}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Sign Out Card */}
      <section className="surface p-6 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 border-rust-900/40 bg-rust-950/10">
        <div>
          <h2 className="text-base font-semibold text-white">Sign out</h2>
          <p className="text-xs text-limestone-400">
            End your current browser session securely
          </p>
        </div>
        <button
          type="button"
          onClick={handleLogout}
          className="button-secondary text-rust-300 hover:text-rust-200 hover:border-rust-800 inline-flex items-center gap-2"
        >
          <LogOut className="w-4 h-4" />
          Sign out
        </button>
      </section>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <AuthGate>
      <SettingsContent />
    </AuthGate>
  );
}
