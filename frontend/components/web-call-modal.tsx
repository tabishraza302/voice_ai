"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { CallPanel } from "./call-panel";
import { api, apiErrorText } from "../lib/api";
import type { CallSessionGrant } from "../lib/types";

export interface WebCallModalProps {
  agentId: string;
  agentName: string;
  languages: string[];
  defaultLanguage?: string | null;
  open: boolean;
  onClose: () => void;
  onSessionEnded?: (sessionId: string) => void;
}

function languageLabel(code: string): string {
  if (code === "en") return "English";
  if (code === "hi") return "Hindi";
  return code;
}

export function WebCallModal({
  agentId,
  agentName,
  languages,
  defaultLanguage,
  open,
  onClose,
  onSessionEnded,
}: WebCallModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [language, setLanguage] = useState(defaultLanguage ?? languages[0] ?? "");
  const [callError, setCallError] = useState<string | null>(null);
  const [latestSessionId, setLatestSessionId] = useState<string | null>(null);

  useEffect(() => {
    setLanguage(defaultLanguage ?? languages[0] ?? "");
  }, [defaultLanguage, languages]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      if (!dialog.open) {
        dialog.showModal();
      }
    } else {
      if (dialog.open) {
        dialog.close();
      }
    }
  }, [open]);

  async function createCall(): Promise<CallSessionGrant> {
    setCallError(null);
    try {
      const grant = await api<CallSessionGrant>(`/agents/${agentId}/test-calls`, {
        method: "POST",
        body: JSON.stringify(language ? { language } : {}),
      });
      setLatestSessionId(grant.session_id);
      return grant;
    } catch (reason) {
      const message = apiErrorText(reason, "Could not start a test call.");
      setCallError(message);
      throw reason;
    }
  }

  function handleCallEnded(sessionId: string) {
    setLatestSessionId(sessionId);
    onSessionEnded?.(sessionId);
  }

  return (
    <dialog
      ref={dialogRef}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === dialogRef.current) {
          onClose();
        }
      }}
      className="m-auto w-[min(94vw,44rem)] rounded-3xl border bg-card p-0 text-card-foreground shadow-2xl backdrop:bg-black/60 backdrop:backdrop-blur-sm"
      aria-label="Web call modal"
    >
      {open ? (
        <div className="flex flex-col overflow-hidden">
          {/* Header */}
          <div className="flex items-center justify-between border-b border-border/80 px-6 py-4 sm:px-7">
            <div className="min-w-0 pr-4">
              <div className="flex items-center gap-2">
                <span className="h-2 w-2 rounded-full bg-emerald-500" aria-hidden="true" />
                <h2 className="text-base font-semibold text-foreground">Web call</h2>
              </div>
              <p className="truncate text-xs text-muted-foreground mt-0.5">{agentName}</p>
            </div>

            <div className="flex items-center gap-3">
              {languages.length > 1 ? (
                <div className="flex items-center gap-1.5">
                  <label htmlFor="web-call-language" className="sr-only">
                    Call language
                  </label>
                  <select
                    id="web-call-language"
                    value={language}
                    onChange={(event) => setLanguage(event.currentTarget.value)}
                    className="h-8 rounded-lg border bg-background px-2.5 text-xs text-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  >
                    {languages.map((item) => (
                      <option key={item} value={item}>
                        {languageLabel(item)}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}

              <button
                type="button"
                onClick={onClose}
                aria-label="Close web call modal"
                className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              >
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          </div>

          {/* Body */}
          <div className="space-y-4">
            {callError ? (
              <p role="alert" className="mx-6 sm:mx-7 mt-4 rounded-xl border border-destructive/30 bg-destructive/10 px-3.5 py-2.5 text-xs text-destructive">
                {callError}
              </p>
            ) : null}

            {latestSessionId ? (
              <div className="mx-6 sm:mx-7 mt-4 flex flex-wrap items-center justify-between gap-2 rounded-xl border bg-muted/40 px-3.5 py-2 text-xs">
                <span className="font-medium text-foreground">Test session trace</span>
                <Link
                  href={`/sessions/${latestSessionId}/trace`}
                  className="rounded-lg font-semibold text-primary underline-offset-4 hover:underline"
                >
                  View scoped trace
                </Link>
              </div>
            ) : null}

            {languages.length === 0 ? (
              <div className="m-6 sm:m-7 rounded-xl border border-dashed p-4 text-xs leading-5 text-muted-foreground">
                Choose a supported language in the Agent Prompt tab before starting a test call.
              </div>
            ) : (
              <CallPanel
                createCall={createCall}
                title={agentName}
                onEnded={handleCallEnded}
                hideHeader
                className="border-0 p-6 sm:p-7 bg-transparent shadow-none"
              />
            )}
          </div>
        </div>
      ) : null}
    </dialog>
  );
}
