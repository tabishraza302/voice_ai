"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { AgentForm } from "@/components/AgentForm";
import { PlatformAgentCreate } from "@/components/PlatformAgentCreate";
import { useAuth } from "@/lib/auth-state";

export default function NewAgentPage() {
  return (
    <Suspense fallback={<div role="status" className="p-6 text-sm text-limestone-400">Loading agent creation…</div>}>
      <NewAgentPageContent />
    </Suspense>
  );
}

function NewAgentPageContent() {
  const { isLocalMode } = useAuth();
  const searchParams = useSearchParams();
  if (isLocalMode) {
    return (
      <div className="max-w-7xl mx-auto animate-in fade-in duration-300">
        <AgentForm isNew={true} />
      </div>
    );
  }
  return (
    <PlatformAgentCreate
      initialMode={searchParams.get("mode") === "generate" ? "generate" : "manual"}
      initialJobId={searchParams.get("job")}
    />
  );
}
