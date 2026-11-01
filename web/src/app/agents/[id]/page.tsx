"use client";

import { Suspense, use } from "react";
import { PlatformAgentWorkspace } from "@/components/PlatformAgentWorkspace";

interface PageProps {
  params: Promise<{ id: string }>;
}

export default function EditAgentPage({ params }: PageProps) {
  return (
    <Suspense fallback={<div role="status" className="p-6 text-sm text-limestone-400">Loading agent workspace…</div>}>
      <EditAgentPageContent params={params} />
    </Suspense>
  );
}

function EditAgentPageContent({ params }: PageProps) {
  const resolvedParams = use(params);
  return <PlatformAgentWorkspace agentId={resolvedParams.id} />;
}
