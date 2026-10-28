"use client";

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { SessionResponse, UserView } from "@/types/auth";
import { platformApi, PlatformApiError } from "@/lib/platform-api-client";

export type AuthStatus =
  | "loading"
  | "anonymous"
  | "mfa_required"
  | "mfa_enrollment_required"
  | "authenticated";

export type AuthState =
  | { status: "loading"; session: null; user: null }
  | { status: "anonymous"; session: SessionResponse; user: null }
  | { status: "mfa_required"; session: SessionResponse; user: UserView | null }
  | { status: "mfa_enrollment_required"; session: SessionResponse; user: UserView | null }
  | { status: "authenticated"; session: SessionResponse; user: UserView };

export function deriveAuthState(session: SessionResponse | null): AuthState {
  if (!session) {
    return { status: "loading", session: null, user: null };
  }
  switch (session.state) {
    case "anonymous":
      return { status: "anonymous", session, user: null };
    case "mfa_required":
      return { status: "mfa_required", session, user: session.user };
    case "mfa_enrollment_required":
      return { status: "mfa_enrollment_required", session, user: session.user };
    case "authenticated":
      if (!session.user) {
        return { status: "anonymous", session, user: null };
      }
      return { status: "authenticated", session, user: session.user };
    default:
      return { status: "anonymous", session, user: null };
  }
}

/**
 * Return path sanitizer enforcing:
 * - Only begins with a single '/'
 * - Rejects '//', schemes (http:, javascript:, data:), and backslashes
 * - Falls back to safe default
 */
export function sanitizeReturnPath(
  rawPath: string | null | undefined,
  fallback: string = "/"
): string {
  if (!rawPath || typeof rawPath !== "string") return fallback;
  const trimmed = rawPath.trim();
  if (
    !trimmed.startsWith("/") ||
    trimmed.startsWith("//") ||
    trimmed.includes("\\")
  ) {
    return fallback;
  }
  try {
    const parsed = new URL(trimmed, "http://localhost");
    if (parsed.origin !== "http://localhost") return fallback;
    if (!parsed.pathname.startsWith("/") || parsed.pathname.startsWith("//")) {
      return fallback;
    }
    return parsed.pathname + parsed.search;
  } catch {
    return fallback;
  }
}

/**
 * Reads token from URL fragment (e.g. #token=...), removes fragment from browser
 * history immediately using replaceState, and returns the token in memory only.
 */
export function extractAndClearFragmentToken(): string | null {
  if (typeof window === "undefined") return null;
  const hash = window.location.hash;
  if (!hash || !hash.startsWith("#")) return null;

  try {
    const params = new URLSearchParams(hash.slice(1));
    const token = params.get("token");
    if (token) {
      const cleanUrl = window.location.pathname + window.location.search;
      window.history.replaceState(null, "", cleanUrl);
      return token;
    }
  } catch {
    // If URL parsing fails, clear hash anyway for safety
    try {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    } catch {
      // Ignore
    }
  }
  return null;
}

/**
 * Detect mode: defaults to 'local' preserving local offline studio workflow.
 * 'saas' mode activates platform auth, account management, and admin gates.
 */
export function getAppMode(): "local" | "saas" {
  const mode = process.env.NEXT_PUBLIC_APP_MODE;
  if (mode === "saas" || mode === "platform") {
    return "saas";
  }
  return "local";
}

/**
 * StepUpManager handles STEP_UP_REQUIRED flows by preserving the pending mutation
 * in memory without submitting it twice.
 */
export class StepUpManager {
  private pendingAction: (() => Promise<unknown>) | null = null;
  private isOpen: boolean = false;
  private isExecuting: boolean = false;
  private listeners = new Set<() => void>();

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private notify() {
    for (const listener of this.listeners) {
      try {
        listener();
      } catch {
        // Ignore listener error
      }
    }
  }

  get isStepUpOpen(): boolean {
    return this.isOpen;
  }

  get hasPendingAction(): boolean {
    return this.pendingAction !== null;
  }

  requestStepUp(action?: () => Promise<unknown>): void {
    if (action) {
      this.pendingAction = action;
    }
    this.isOpen = true;
    this.notify();
  }

  async resolveStepUp(
    stepUpCall: () => Promise<SessionResponse>
  ): Promise<unknown> {
    await stepUpCall();
    this.isOpen = false;

    if (this.pendingAction && !this.isExecuting) {
      this.isExecuting = true;
      const action = this.pendingAction;
      this.pendingAction = null; // Clear immediately to prevent double submission
      this.notify();
      try {
        return await action();
      } finally {
        this.isExecuting = false;
      }
    }
    this.notify();
    return undefined;
  }

  cancelStepUp(): void {
    this.pendingAction = null;
    this.isOpen = false;
    this.notify();
  }
}

export interface AuthContextValue {
  state: AuthState;
  appMode: "local" | "saas";
  isLocalMode: boolean;
  refreshSession: () => Promise<SessionResponse | null>;
  logout: () => Promise<void>;
  stepUpManager: StepUpManager;
  executeWithStepUp: <T>(action: () => Promise<T>) => Promise<T>;
}

const defaultStepUpManager = new StepUpManager();

export const AuthContext = createContext<AuthContextValue>({
  state: { status: "loading", session: null, user: null },
  appMode: "local",
  isLocalMode: true,
  refreshSession: async () => null,
  logout: async () => {},
  stepUpManager: defaultStepUpManager,
  executeWithStepUp: async (action) => action(),
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [stepUpManager] = useState(() => new StepUpManager());
  const appMode = useMemo(() => getAppMode(), []);
  const isLocalMode = appMode === "local";

  const refreshSession = useCallback(async (): Promise<SessionResponse | null> => {
    if (isLocalMode) {
      return null;
    }
    try {
      const freshSession = await platformApi.getSession();
      setSession(freshSession);
      return freshSession;
    } catch (err) {
      if (err instanceof PlatformApiError && err.status === 401) {
        // If unauthenticated or expired, fetch anonymous session
        try {
          const anon = await platformApi.getSession();
          setSession(anon);
          return anon;
        } catch {
          setSession(null);
          return null;
        }
      }
      return null;
    }
  }, [isLocalMode]);

  useEffect(() => {
    if (isLocalMode) {
      return;
    }

    // Subscribe to platform API client session state changes
    const unsubscribe = platformApi.onSessionChange((updatedSession) => {
      setSession(updatedSession);
    });

    // Initial session bootstrap
    refreshSession();

    return () => {
      unsubscribe();
    };
  }, [isLocalMode, refreshSession]);

  const logout = useCallback(async () => {
    if (isLocalMode) return;
    try {
      await platformApi.logout();
    } catch {
      // Clear session even if logout API errored
    } finally {
      // Re-bootstrap anonymous session
      await refreshSession();
    }
  }, [isLocalMode, refreshSession]);

  const executeWithStepUp = useCallback(
    async <T,>(action: () => Promise<T>): Promise<T> => {
      try {
        return await action();
      } catch (err) {
        if (err instanceof PlatformApiError && err.code === "STEP_UP_REQUIRED") {
          const { promise, resolve, reject } = Promise.withResolvers<T>();
          stepUpManager.requestStepUp(async () => {
            try {
              const result = await action();
              resolve(result);
            } catch (retryErr) {
              reject(retryErr);
            }
          });
          return promise;
        }
        throw err;
      }
    },
    [stepUpManager]
  );

  const authState = useMemo<AuthState>(() => {
    if (isLocalMode) {
      // In local mode, treat as pseudo-authenticated local workspace without backend credentials
      return {
        status: "authenticated",
        session: {
          state: "authenticated",
          user: {
            id: "local-user",
            email: "local@localhost",
            display_name: "Local Studio",
            role: "admin",
            status: "active",
            email_verified_at: new Date(0).toISOString(),
            preferred_language: "en",
            timezone: "UTC",
            low_balance_notifications: false,
            mfa_enabled: false,
            created_at: new Date(0).toISOString(),
            updated_at: new Date(0).toISOString(),
          },
          session_id: "local-session",
          expires_at: new Date(Date.now() + 86400000).toISOString(),
          csrf_token: "local-csrf",
          step_up_expires_at: null,
        },
        user: {
          id: "local-user",
          email: "local@localhost",
          display_name: "Local Studio",
          role: "admin",
          status: "active",
          email_verified_at: new Date(0).toISOString(),
          preferred_language: "en",
          timezone: "UTC",
          low_balance_notifications: false,
          mfa_enabled: false,
          created_at: new Date(0).toISOString(),
          updated_at: new Date(0).toISOString(),
        },
      };
    }
    return deriveAuthState(session);
  }, [isLocalMode, session]);

  const value = useMemo<AuthContextValue>(
    () => ({
      state: authState,
      appMode,
      isLocalMode,
      refreshSession,
      logout,
      stepUpManager,
      executeWithStepUp,
    }),
    [
      authState,
      appMode,
      isLocalMode,
      refreshSession,
      logout,
      stepUpManager,
      executeWithStepUp,
    ]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}
