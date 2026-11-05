import { AdminPage } from "../../../components/admin/admin-ui";
import { UsersDirectory } from "../../../components/admin/users-directory";

export default function UsersPage() {
  return (
    <AdminPage
      title="Users"
      description="View account roles and aggregate usage counts. This directory does not include transcripts, answers, or agent instructions."
    >
      <UsersDirectory />
    </AdminPage>
  );
}
