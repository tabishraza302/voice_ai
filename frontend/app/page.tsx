import Link from "next/link";

export default function HomePage() {
  return (
    <main className="mx-auto grid min-h-dvh max-w-7xl content-between px-4 py-8 sm:px-6 lg:px-8">
      <header className="flex items-center justify-between gap-4">
        <Link href="/" className="rounded-xl text-sm font-semibold tracking-tight focus-visible:outline-offset-4">
          Voice Agent Studio
        </Link>
        <nav aria-label="Account" className="flex items-center gap-2">
          <Link href="/login" className="rounded-xl px-3 py-2 text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground">
            Sign in
          </Link>
          <Link
            href="/register"
            className="inline-flex min-h-11 items-center justify-center whitespace-nowrap rounded-xl bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            Create account
          </Link>
</nav>
      </header>
      <section className="grid gap-10 py-16 md:grid-cols-[minmax(0,1fr)_minmax(18rem,0.65fr)] md:items-center md:py-24">
        <div className="max-w-2xl space-y-6">
          <p className="text-sm font-semibold text-primary">Voice agent workspace</p>
          <h1 className="text-4xl font-semibold leading-tight tracking-tight sm:text-5xl lg:text-6xl">
            Design the conversation, then test it out loud.
          </h1>
          <p className="max-w-xl text-base leading-7 text-muted-foreground">
            Build an agent from your own questions and rules. Run a test call, then review the session in your workspace.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <Link
              href="/register"
              className="inline-flex min-h-11 items-center justify-center whitespace-nowrap rounded-xl bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            >
              Create account
            </Link>
            <Link href="/login" className="rounded-xl px-4 py-3 text-sm font-medium text-foreground underline-offset-4 hover:underline">
              Sign in to your workspace
            </Link>
          </div>
        </div>
        <div className="rounded-2xl border bg-card p-6 shadow-panel sm:p-8">
          <h2 className="text-lg font-semibold tracking-tight">A clear path from setup to review</h2>
          <ol className="mt-5 space-y-4 text-sm leading-6 text-muted-foreground">
            <li><span className="font-medium text-foreground">Configure</span> the agent and its question flow.</li>
            <li><span className="font-medium text-foreground">Test</span> the conversation with a live call.</li>
            <li><span className="font-medium text-foreground">Review</span> session history and available diagnostics.</li>
          </ol>
        </div>
      </section>
      <footer className="border-t py-5 text-sm text-muted-foreground">
        Voice Agent Studio
      </footer>
    </main>
  );
}
