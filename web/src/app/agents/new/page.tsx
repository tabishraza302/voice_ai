"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { PlatformAgentCreate } from "@/components/PlatformAgentCreate";

export default function NewAgentPage() {
  return (
    <Suspense fallback={<div role="status" className="p-6 text-sm text-limestone-400">Loading agent creation…</div>}>
      <NewAgentPageContent />
    </Suspense>
  );
}

function NewAgentPageContent() {
  const searchParams = useSearchParams();
  return (
    <PlatformAgentCreate
      initialMode={searchParams.get("mode") === "generate" ? "generate" : "manual"}
      initialJobId={searchParams.get("job")}
    />
  );
}
