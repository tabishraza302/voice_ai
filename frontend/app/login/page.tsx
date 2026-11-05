"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { ApiError, api } from "@/lib/api";
import type { CurrentUser } from "@/lib/types";
import { AuthFrame } from "@/components/ui/auth-frame";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [devLoginAvailable, setDevLoginAvailable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api<{ enabled: boolean }>("/auth/dev-login/available")
      .then(() => { if (!cancelled) setDevLoginAvailable(true); })
      .catch(() => { if (!cancelled) setDevLoginAvailable(false); });
    return () => { cancelled = true; };
  }, []);

  async function devSignIn(role: "user" | "admin") {
    setError(null);
    setPending(true);
    try {
      const user = await api<CurrentUser>("/auth/dev-login", {
        method: "POST",
        body: JSON.stringify({ role }),
      });
      router.replace(user.role === "admin" ? "/admin" : "/dashboard");
      router.refresh();
    } catch (reason) {
      setError(reason instanceof ApiError
        ? reason.status > 0 ? `${reason.message} (HTTP ${reason.status})` : reason.message
        : "Could not sign in as the dev account. Try again.");
      setPending(false);
    }
  }

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setPending(true);
    try {
      const user = await api<CurrentUser>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      router.replace(user.role === "admin" ? "/admin" : "/dashboard");
      router.refresh();
    } catch (reason) {
      setError(reason instanceof ApiError
        ? reason.status > 0 ? `${reason.message} (HTTP ${reason.status})` : reason.message
        : "Could not sign in. Try again.");
      setPending(false);
    }
  }

  return (
    <AuthFrame
      title="Sign in"
      description="Use your account email and password to continue."
      alternateLabel="New to this workspace?"
      alternateHref="/register"
      alternateAction="Create an account"
    >
      <form className="space-y-5" onSubmit={signIn} noValidate>
        {error ? <p role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 px-3.5 py-3 text-sm text-destructive">{error}</p> : null}
        <div className="space-y-2">
          <Label htmlFor="email">Email</Label>
          <Input
            id="email"
            name="email"
            type="email"
            autoComplete="email"
            maxLength={320}
            required
            value={email}
            onChange={(event) => setEmail(event.currentTarget.value)}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="password">Password</Label>
          <Input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            minLength={12}
            maxLength={1024}
            required
            value={password}
            onChange={(event) => setPassword(event.currentTarget.value)}
          />
        </div>
        <Button className="w-full" type="submit" disabled={pending || !email.trim() || password.length < 12}>
          {pending ? "Signing in..." : "Sign in"}
        </Button>
      </form>
      {devLoginAvailable ? (
        <div className="mt-5 space-y-3">
          <div className="relative flex items-center">
            <span aria-hidden="true" className="absolute inset-x-0 h-px bg-border" />
            <span className="relative mx-auto bg-background px-3 text-xs uppercase tracking-wide text-muted-foreground">Development</span>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Button variant="outline" type="button" disabled={pending} onClick={() => void devSignIn("user")}>
              Sign in as User
            </Button>
            <Button variant="outline" type="button" disabled={pending} onClick={() => void devSignIn("admin")}>
              Sign in as Admin
            </Button>
          </div>
          <p className="text-center text-[11px] leading-4 text-muted-foreground">Local development only. Seeded dev accounts — not for production.</p>
        </div>
      ) : null}
      <p className="mt-6 text-center text-sm text-muted-foreground">
        <Link href="/" className="rounded-xl underline-offset-4 hover:text-foreground hover:underline">Return to home</Link>
      </p>
    </AuthFrame>
  );
}
