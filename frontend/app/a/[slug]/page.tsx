"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { CallPanel, type CallSessionGrant } from "../../../components/call-panel";
import { api } from "../../../lib/api";

type PublicAgent = {
  name: string;
  slug: string;
  description: string | null;
  languages: string[];
  default_language: string | null;
};

type AgentLoadState =
  | { kind: "loading" }
  | { kind: "ready"; agent: PublicAgent }
  | { kind: "unavailable"; message: string };

function parsePublicAgent(value: unknown, expectedSlug: string): PublicAgent | null {
  if (
    typeof value !== "object" ||
    value === null ||
    Array.isArray(value) ||
    !("name" in value) ||
    !("slug" in value) ||
    !("description" in value) ||
    !("languages" in value) ||
    !("default_language" in value)
  ) {
    return null;
  }

  if (
    typeof value.name !== "string" ||
    value.name.trim().length === 0 ||
    value.slug !== expectedSlug ||
    !(typeof value.description === "string" || value.description === null) ||
    !Array.isArray(value.languages) ||
    !value.languages.every((language) => typeof language === "string" && language.trim().length > 0) ||
    !(typeof value.default_language === "string" || value.default_language === null)
  ) {
    return null;
  }

  if (value.default_language !== null && !value.languages.includes(value.default_language)) return null;

  return {
    name: value.name,
    slug: value.slug,
    description: value.description,
    languages: value.languages,
    default_language: value.default_language,
  };
}

function languageLabel(language: string): string {
  const normalized = language.toLowerCase();
  if (normalized === "en") return "English";
  if (normalized === "hi") return "Hindi";
  return language.toUpperCase();
}

export default function PublicCallPage() {
  const params = useParams<{ slug: string }>();
  const slug = typeof params.slug === "string" ? params.slug : "";
  const [state, setState] = useState<AgentLoadState>({ kind: "loading" });
  const [selectedLanguage, setSelectedLanguage] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!slug) {
      setState({ kind: "unavailable", message: "This agent is not available for calls." });
      return;
    }

    const controller = new AbortController();
    let cancelled = false;
    setState({ kind: "loading" });
    void api<unknown>(`/public/agents/${encodeURIComponent(slug)}`, { signal: controller.signal })
      .then((response) => {
        if (cancelled) return;
        const agent = parsePublicAgent(response, slug);
        setState(
          agent
            ? { kind: "ready", agent }
            : { kind: "unavailable", message: "This agent is not available for calls." },
        );
      })
      .catch((error: unknown) => {
        if (cancelled || controller.signal.aborted) return;
        const status =
          typeof error === "object" && error !== null && "status" in error ? error.status : null;
        setState({
          kind: "unavailable",
          message:
            status === 404
              ? "This agent is not available for calls."
              : "We couldn't load this agent. Check your connection and try again.",
        });
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [slug, reloadKey]);

  const agent = state.kind === "ready" ? state.agent : null;
  const language =
    agent === null
      ? ""
      : selectedLanguage !== null && agent.languages.includes(selectedLanguage)
        ? selectedLanguage
        : agent.default_language ?? agent.languages[0] ?? "";

  async function createPublicCall(): Promise<CallSessionGrant> {
    if (!agent || !language) throw new Error("Choose a language before starting the call.");
    return api<CallSessionGrant>(`/public/agents/${encodeURIComponent(agent.slug)}/calls`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ language }),
    });
  }

  return (
    <main className="min-h-screen bg-[#f7f6f0] px-4 py-10 text-stone-900 sm:px-6 sm:py-16">
      <div className="mx-auto w-full max-w-3xl">
        {state.kind === "loading" ? (
          <div className="rounded-3xl border border-stone-200 bg-white p-8 text-stone-700" role="status" aria-live="polite">
            <span className="mr-3 inline-block h-3 w-3 animate-pulse rounded-full bg-emerald-700" aria-hidden="true" />
            Loading this agent…
          </div>
        ) : null}

        {state.kind === "unavailable" ? (
          <section className="rounded-3xl border border-stone-200 bg-white p-7 shadow-[0_16px_40px_-28px_rgba(24,39,34,0.35)] sm:p-9" aria-labelledby="unavailable-title">
            <h1 id="unavailable-title" className="text-2xl font-semibold tracking-[-0.03em] sm:text-3xl">
              This call isn’t available
            </h1>
            <p className="mt-3 max-w-prose text-sm leading-6 text-stone-600" role="alert">
              {state.message}
            </p>
            <button
              type="button"
              onClick={() => setReloadKey((value) => value + 1)}
              className="mt-6 inline-flex min-h-11 items-center justify-center rounded-full bg-emerald-800 px-5 text-sm font-semibold text-white transition-colors hover:bg-emerald-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2"
            >
              Try again
            </button>
          </section>
        ) : null}

        {agent ? (
          <>
            <header className="mb-8 sm:mb-10">
              <p className="text-sm font-medium text-emerald-900">Voice call</p>
              <h1 className="mt-2 text-4xl font-semibold tracking-[-0.04em] sm:text-5xl">{agent.name}</h1>
              {agent.description ? (
                <p className="mt-4 max-w-2xl text-base leading-7 text-stone-700">{agent.description}</p>
              ) : null}
            </header>

            {agent.languages.length > 0 ? (
              <>
                <div className="mb-4 rounded-2xl border border-stone-200 bg-white px-4 py-4 sm:px-5">
                  <label htmlFor="call-language" className="block text-sm font-semibold text-stone-800">
                    Language for this call
                  </label>
                  {agent.languages.length === 1 ? (
                    <p className="mt-2 text-sm text-stone-600">{languageLabel(agent.languages[0])}</p>
                  ) : (
                    <select
                      id="call-language"
                      value={language}
                      onChange={(event) => setSelectedLanguage(event.target.value)}
                      className="mt-2 min-h-11 w-full rounded-xl border border-stone-300 bg-white px-3 text-sm text-stone-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 sm:max-w-xs"
                    >
                      {agent.languages.map((option) => (
                        <option key={option} value={option}>{languageLabel(option)}</option>
                      ))}
                    </select>
                  )}
                  <p className="mt-2 text-xs leading-5 text-stone-500">The language is set when the call starts.</p>
                </div>
                <CallPanel title={`Talk with ${agent.name}`} createCall={createPublicCall} />
              </>
            ) : (
              <p className="rounded-2xl border border-stone-200 bg-white p-5 text-sm leading-6 text-stone-700" role="alert">
                Voice calling is not available for this agent right now.
              </p>
            )}
          </>
        ) : null}
      </div>
    </main>
  );
}
