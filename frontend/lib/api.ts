export class ApiError extends Error {
  readonly status: number;
  readonly details: unknown;

  constructor(status: number, message: string, details: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

const API_ROOT = "/api";
const SAFE_METHODS: Record<string, true> = { GET: true, HEAD: true, OPTIONS: true };

function requiresCsrf(route: string, method: string): boolean {
  if (SAFE_METHODS[method]) return false;
  // Session-establishing routes cannot present a CSRF token beforehand.
  if (route === "/auth/login" || route === "/auth/register" || route === "/auth/dev-login") return false;
  // Public calls and signaling are authorized by single-use grants and
  // renegotiation tokens, not by the owner's cookie. Requiring /auth/me here
  // would make anonymous callers fail before WebRTC starts.
  if (route.startsWith("/public/") || route.startsWith("/signaling/")) return false;
  return true;
}

function apiUrl(path: string): string {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new TypeError("API paths must be same-origin paths beginning with a single slash");
  }
  return `${API_ROOT}${path}`;
}

function errorMessage(payload: unknown, fallback: string): { message: string; details: unknown } {
  if (typeof payload === "string" && payload.trim()) return { message: payload, details: payload };
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return { message: fallback, details: payload };
  }

  let detail: unknown;
  if ("detail" in payload) detail = payload.detail;
  if (typeof detail === "string" && detail.trim()) return { message: detail, details: detail };
  if ("message" in payload && typeof payload.message === "string" && payload.message.trim()) {
    return { message: payload.message, details: detail ?? payload };
  }

  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const messages = Object.entries(detail).flatMap(([field, value]) => {
      const entries = typeof value === "string" ? [value] : Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
      return entries.map((message) => `${field}: ${message}`);
    });
    if (messages.length) return { message: messages.join(" "), details: detail };
  }
  return { message: fallback, details: detail ?? payload };
}

async function responsePayload(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const text = await response.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function throwResponseError(response: Response, payload: unknown): never {
  const { message, details } = errorMessage(payload, response.statusText || "The request could not be completed.");
  throw new ApiError(response.status, message, details);
}

async function csrfToken(signal?: AbortSignal): Promise<string> {
  let response: Response;
  try {
    response = await fetch(apiUrl("/auth/me"), {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal,
    });
  } catch {
    throw new ApiError(0, "Could not connect to the service. Try again.");
  }
  const payload = await responsePayload(response);
  if (!response.ok) throwResponseError(response, payload);
  if (!payload || typeof payload !== "object" || Array.isArray(payload) || !("csrf_token" in payload) || typeof payload.csrf_token !== "string") {
    throw new ApiError(response.status, "Your session could not be verified. Sign in again.");
  }
  return payload.csrf_token;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const url = apiUrl(path);
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");

  if (init.body != null && typeof FormData !== "undefined" && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const route = path.split("?", 1)[0];
  if (requiresCsrf(route, method)) {
    headers.set("X-CSRF-Token", await csrfToken(init.signal ?? undefined));
  }

  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      method,
      headers,
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "Could not connect to the service. Try again.");
  }

  const payload = await responsePayload(response);
  if (!response.ok) throwResponseError(response, payload);
  return payload as T;
}

export function apiErrorText(reason: unknown, fallback: string): string {
  if (!(reason instanceof ApiError)) return fallback;
  return reason.status > 0 ? `${reason.message} (HTTP ${reason.status})` : reason.message;
}

export function apiFieldErrors(error: ApiError): Record<string, string[]> {
  const detail = error.details;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return {};
  return Object.fromEntries(
    Object.entries(detail).flatMap(([field, value]) => {
      const messages = typeof value === "string" ? [value] : Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
      return messages.length > 0 ? [[field, messages]] : [];
    }),
  );
}
