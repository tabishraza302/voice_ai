import Link from "next/link";
import { buttonClassName } from "@/components/ui/button";

export default function HomePage() {
  return (
    <main className="mx-auto grid grid-cols-1 min-h-dvh max-w-7xl content-center px-4 py-8 sm:px-6 lg:px-8">
      <header className="absolute inset-x-0 top-0">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-5 sm:px-6 lg:px-8">
          <Link href="/" aria-label="Voice Agent Studio" className="flex shrink-0 items-center gap-2 rounded-xl text-sm font-semibold tracking-tight focus-visible:outline-offset-4">
            <span aria-hidden="true" className="grid size-8 place-items-center rounded-xl bg-primary text-xs font-bold text-primary-foreground">V</span>
            <span className="hidden min-[430px]:inline">Voice Agent Studio</span>
          </Link>
          <nav aria-label="Account" className="flex items-center gap-2">
            <Link href="/login" className="rounded-xl px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
              Sign in
            </Link>
            <Link href="/register" className={buttonClassName("primary", "default", "whitespace-nowrap")}>
              Create account
            </Link>
          </nav>
        </div>
      </header>
      <section className="grid grid-cols-1 gap-10 pt-16 md:grid-cols-[minmax(0,1fr)_minmax(18rem,0.65fr)] md:items-center">
        <div className="max-w-2xl space-y-6">
          <h1 className="text-4xl font-semibold leading-tight tracking-tight sm:text-5xl lg:text-6xl">
            Design the conversation, then test it out loud.
          </h1>
          <p className="max-w-xl text-base leading-7 text-muted-foreground">
            Build an agent from your own questions and rules. Run a test call, then review the session in your workspace.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <Link href="/register" className={buttonClassName("primary", "default", "whitespace-nowrap")}>
              Create account
            </Link>
            <Link href="/login" className="rounded-xl px-4 py-3 text-sm font-medium text-foreground underline-offset-4 hover:underline">
              Sign in to your workspace
            </Link>
          </div>
        </div>
        <div className="rounded-2xl border bg-card p-6 sm:p-8">
          <h2 className="text-lg font-semibold tracking-tight">From setup to review</h2>
          <ol className="mt-5 space-y-4 text-sm leading-6 text-muted-foreground">
            <li><span className="font-medium text-foreground">Configure</span> the agent and its question flow.</li>
            <li><span className="font-medium text-foreground">Test</span> the conversation with a live call.</li>
            <li><span className="font-medium text-foreground">Review</span> session history and diagnostics.</li>
          </ol>
        </div>
      </section>
    </main>
  );
}
