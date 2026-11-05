import { AdminPage } from "../../../components/admin/admin-ui";
import { AgentsDirectory } from "../../../components/admin/agents-directory";

export default function AgentsPage() {
  return (
    <AdminPage
      title="Agents"
      description="View owners, status, and aggregate counts. Suspending an agent blocks new calls without ending active calls."
    >
      <AgentsDirectory />
    </AdminPage>
  );
}
