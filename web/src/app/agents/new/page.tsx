import { AgentForm } from "@/components/AgentForm";

export default function NewAgentPage() {
  return (
    <div className="max-w-7xl mx-auto animate-in fade-in duration-300">
      <AgentForm isNew={true} />
    </div>
  );
}
