"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { ApiError, api } from "@/lib/api";
import type { Agent, CurrentUser } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { OwnerShell } from "@/app/dashboard/owner-shell";

function toSlug(value: string): string {
  return value.toLocaleLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

export default function NewAgentPage() {
  const router = useRouter();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [checking, setChecking] = useState(true);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let current = true;
    api<CurrentUser>("/auth/me").then((currentUser) => {
      if (!current) return;
      if (currentUser.role === "admin") {
        router.replace("/admin");
        return;
      }
      setUser(currentUser);
      setChecking(false);
    }).catch((reason: unknown) => {
      if (!current) return;
      if (reason instanceof ApiError && reason.status === 401) router.replace("/login");
      else {
        setError(reason instanceof ApiError ? reason.message : "Could not verify your account.");
        setChecking(false);
      }
    });
    return () => { current = false; };
  }, [router]);

  async function createDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      const agent = await api<Agent>("/agents", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), slug: slug.trim(), description: description.trim() || null }),
      });
      router.replace(`/agents/${agent.id}`);
    } catch (reason) {
      setError(reason instanceof ApiError
        ? reason.status > 0 ? `${reason.message} (HTTP ${reason.status})` : reason.message
        : "Could not save this draft. Try again.");
      setSaving(false);
    }
  }

  return (
    <OwnerShell active="agents" email={user?.email}>
      <main className="mx-auto max-w-3xl px-4 py-8 sm:px-6 sm:py-10 lg:px-8">
        <Link href="/dashboard" className="rounded-xl text-sm font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">
          Back to dashboard
        </Link>
        <div className="mt-7 space-y-2">
          <p className="text-sm font-medium text-primary">New agent</p>
          <h1 className="text-3xl font-semibold tracking-tight">Start with a draft</h1>
          <p className="max-w-2xl text-sm leading-6 text-muted-foreground">
            Save a name and a unique URL slug now. Voice settings and questions can be added later.
          </p>
        </div>
        <Card className="mt-7">
          <CardContent className="pt-6">
            {checking ? (
              <p role="status" className="text-sm text-muted-foreground">Checking your account...</p>
            ) : error ? (
              <div className="space-y-3" role="alert">
                <p className="text-sm text-destructive">{error}</p>
                <Button variant="outline" onClick={() => router.refresh()}>Try again</Button>
              </div>
            ) : (
              <form className="space-y-5" onSubmit={createDraft}>
                {error ? <p role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 px-3.5 py-3 text-sm text-destructive">{error}</p> : null}
                <div className="space-y-2">
                  <Label htmlFor="agent-name">Agent name</Label>
                  <Input
                    id="agent-name"
                    name="name"
                    autoComplete="off"
                    maxLength={120}
                    required
                    value={name}
                    onChange={(event) => {
                      const next = event.currentTarget.value;
                      setName(next);
                      if (!slugEdited) setSlug(toSlug(next));
                    }}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="agent-slug">Public URL slug</Label>
                  <div className="flex min-h-11 items-center gap-2 rounded-xl border bg-background px-3.5 focus-within:ring-2 focus-within:ring-ring">
                    <span className="shrink-0 text-sm text-muted-foreground">/a/</span>
                    <input
                      id="agent-slug"
                      name="slug"
                      className="min-w-0 flex-1 bg-transparent py-2 text-sm outline-none"
                      autoComplete="off"
                      maxLength={80}
                      pattern="[a-z0-9]+(-[a-z0-9]+)*"
                      required
                      value={slug}
                      onChange={(event) => {
                        setSlugEdited(true);
                        setSlug(event.currentTarget.value.toLocaleLowerCase().replace(/[^a-z0-9-]/g, ""));
                      }}
                      aria-describedby="agent-slug-help"
                    />
                  </div>
                  <p id="agent-slug-help" className="text-sm leading-5 text-muted-foreground">
                    Use lowercase letters, numbers, and single hyphens. Slugs are unique.
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="agent-description">Description <span className="font-normal text-muted-foreground">(optional)</span></Label>
                  <Textarea
                    id="agent-description"
                    name="description"
                    maxLength={500}
                    value={description}
                    onChange={(event) => setDescription(event.currentTarget.value)}
                  />
                </div>
                <div className="flex flex-col-reverse gap-3 border-t pt-5 sm:flex-row sm:justify-end">
                  <Link href="/dashboard" className="inline-flex min-h-11 items-center justify-center rounded-xl border px-4 py-2.5 text-sm font-medium hover:bg-muted">Cancel</Link>
                  <Button type="submit" disabled={saving || !name.trim() || !slug.trim()}>
                    {saving ? "Saving draft..." : "Create draft"}
                  </Button>
                </div>
              </form>
            )}
          </CardContent>
        </Card>
      </main>
    </OwnerShell>
  );
}
