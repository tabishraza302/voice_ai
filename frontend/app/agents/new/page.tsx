"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { ApiError, api, apiErrorText } from "@/lib/api";
import type { Agent, CurrentUser } from "@/lib/types";
import { Button, buttonClassName } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { OwnerShell } from "@/app/dashboard/owner-shell";
import { PromptGenerationView } from "@/components/agents/prompt-generation-view";

export default function NewAgentPage() {
  const router = useRouter();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [checking, setChecking] = useState(true);
  const [creationMode, setCreationMode] = useState<"ai" | "blank">("ai");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let current = true;
    api<CurrentUser>("/auth/me")
      .then((currentUser) => {
        if (!current) return;
        if (currentUser.role === "admin") {
          router.replace("/admin");
          return;
        }
        setUser(currentUser);
        setChecking(false);
      })
      .catch((reason: unknown) => {
        if (!current) return;
        if (reason instanceof ApiError && reason.status === 401) {
          router.replace("/login");
        } else {
          setError(reason instanceof ApiError ? reason.message : "Could not verify your account.");
          setChecking(false);
        }
      });
    return () => {
      current = false;
    };
  }, [router]);

  async function createDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      const agent = await api<Agent>("/agents", {
        method: "POST",
        body: JSON.stringify({ name: name.trim() }),
      });
      router.replace(`/agents/${agent.id}`);
    } catch (reason) {
      setError(apiErrorText(reason, "Could not save this draft. Try again."));
      setSaving(false);
    }
  }

  return (
    <OwnerShell active="agents" email={user?.email}>
      <main className="mx-auto max-w-3xl px-4 py-8 sm:px-6 sm:py-10 lg:px-8">
        <Link
          href="/dashboard"
          className="rounded-xl text-sm font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          Back to dashboard
        </Link>

        <div className="mt-7 space-y-2">
          <h1 className="text-3xl font-semibold tracking-tight">
            {creationMode === "ai" ? "Create agent with AI" : "Start with a draft"}
          </h1>
          <p className="max-w-2xl text-sm leading-6 text-muted-foreground">
            {creationMode === "ai"
              ? "Describe your voice agent and our AI will generate its name, instructions, and call flow steps."
              : "Name your agent to start configuring its voice, instructions, and questions from a blank canvas."}
          </p>
        </div>

        {/* Tab switcher */}
        <div className="mt-6 flex items-center p-1 rounded-xl bg-muted/50 border max-w-fit">
          <button
            type="button"
            onClick={() => setCreationMode("ai")}
            className={`flex items-center gap-2 px-4 py-2 text-xs sm:text-sm font-medium rounded-lg transition-all ${
              creationMode === "ai"
                ? "bg-background text-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <svg
              className={`w-4 h-4 ${creationMode === "ai" ? "text-primary" : "text-muted-foreground"}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
            Generate with AI
          </button>
          <button
            type="button"
            onClick={() => setCreationMode("blank")}
            className={`flex items-center gap-2 px-4 py-2 text-xs sm:text-sm font-medium rounded-lg transition-all ${
              creationMode === "blank"
                ? "bg-background text-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <svg
              className={`w-4 h-4 ${creationMode === "blank" ? "text-primary" : "text-muted-foreground"}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
              />
            </svg>
            Start from blank
          </button>
        </div>

        <Card className="mt-6">
          <CardContent className="pt-6">
            {checking ? (
              <p role="status" className="text-sm text-muted-foreground">
                Checking your account...
              </p>
            ) : error ? (
              <div className="space-y-3" role="alert">
                <p className="text-sm text-destructive">{error}</p>
                <Button variant="outline" onClick={() => router.refresh()}>
                  Try again
                </Button>
              </div>
            ) : (
              <>
                <div className={creationMode === "ai" ? "block" : "hidden"}>
                  <PromptGenerationView onCancel={() => router.push("/dashboard")} />
                </div>
                <div className={creationMode === "blank" ? "block" : "hidden"}>
                  <form className="space-y-5" onSubmit={createDraft}>
                    {error ? (
                      <p
                        role="alert"
                        className="rounded-xl border border-destructive/30 bg-destructive/10 px-3.5 py-3 text-sm text-destructive"
                      >
                        {error}
                      </p>
                    ) : null}
                    <div className="space-y-2">
                      <Label htmlFor="agent-name">Agent name</Label>
                      <Input
                        id="agent-name"
                        name="name"
                        autoComplete="off"
                        maxLength={120}
                        required
                        value={name}
                        onChange={(event) => setName(event.currentTarget.value)}
                      />
                    </div>
                    <div className="flex flex-col-reverse gap-3 border-t pt-5 sm:flex-row sm:justify-end">
                      <Link href="/dashboard" className={buttonClassName("outline")}>
                        Cancel
                      </Link>
                      <Button type="submit" disabled={saving || !name.trim()}>
                        {saving ? "Saving draft..." : "Create draft"}
                      </Button>
                    </div>
                  </form>
                </div>
              </>
            )}
          </CardContent>
        </Card>
      </main>
    </OwnerShell>
  );
}
