"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { api, apiErrorText } from "@/lib/api";
import type { CurrentUser } from "@/lib/types";
import { AuthFrame } from "@/components/ui/auth-frame";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [created, setCreated] = useState(false);

  async function register(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setPending(true);
    try {
      await api<CurrentUser>("/auth/register", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      setCreated(true);
      const user = await api<CurrentUser>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      router.replace(user.role === "admin" ? "/admin" : "/dashboard");
      router.refresh();
    } catch (reason) {
      setError(apiErrorText(reason, created
        ? "Your account was created, but sign-in could not finish. Sign in with your new account."
        : "Could not create your account. Try again."));
      setPending(false);
    }
  }

  return (
    <AuthFrame
      title="Create your account"
      description="Your account starts with owner access. Administrator access is managed separately."
      alternateLabel="Already have an account?"
      alternateHref="/login"
      alternateAction="Sign in"
    >
      <form className="space-y-5" onSubmit={register} noValidate>
        {error ? <p role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 px-3.5 py-3 text-sm text-destructive">{error}</p> : null}
        {created && error ? <p role="status" className="text-sm text-muted-foreground">Your account was created.</p> : null}
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
            autoComplete="new-password"
            minLength={12}
            maxLength={1024}
            required
            value={password}
            onChange={(event) => setPassword(event.currentTarget.value)}
          />
          <p className="text-sm leading-5 text-muted-foreground">Use at least 12 characters.</p>
        </div>
        <Button className="w-full" type="submit" disabled={pending || !email.trim() || password.length < 12}>
          {pending ? "Creating account..." : "Create account"}
        </Button>
      </form>
    </AuthFrame>
  );
}
