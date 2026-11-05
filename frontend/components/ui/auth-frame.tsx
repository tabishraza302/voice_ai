import Link from "next/link";
import type { ReactNode } from "react";

export function AuthFrame({
  children,
  title,
  description,
  alternateLabel,
  alternateHref,
  alternateAction,
}: {
  children: ReactNode;
  title: string;
  description: string;
  alternateLabel: string;
  alternateHref: string;
  alternateAction: string;
}) {
  return (
    <main className="mx-auto grid grid-cols-1 min-h-dvh max-w-6xl items-center gap-10 px-4 py-10 sm:px-6 md:grid-cols-[1fr_0.9fr] md:gap-16 lg:px-8">
      <aside className="space-y-10">
        <Link href="/" className="inline-flex rounded-xl text-sm font-semibold tracking-tight focus-visible:outline-offset-4">
          Voice Agent Studio
        </Link>
        <div className="max-w-lg">
          <h1 className="max-w-md text-4xl font-semibold leading-tight tracking-tight sm:text-5xl">
            Build and manage voice agents.
          </h1>
          <p className="mt-5 max-w-md text-base leading-7 text-muted-foreground">
            Configure questions, test a call, and review sessions from one owner workspace.
          </p>
        </div>
        <p className="text-sm text-muted-foreground">
          {alternateLabel}{" "}
          <Link href={alternateHref} className="font-semibold text-primary underline-offset-4 hover:underline">
            {alternateAction}
          </Link>
        </p>
      </aside>
      <section className="w-full rounded-2xl border bg-card p-6 sm:p-8" aria-labelledby="auth-title">
        <div className="mb-7 space-y-2">
          <h2 id="auth-title" className="text-2xl font-semibold tracking-tight">{title}</h2>
          <p className="text-sm leading-6 text-muted-foreground">{description}</p>
        </div>
        {children}
      </section>
    </main>
  );
}
