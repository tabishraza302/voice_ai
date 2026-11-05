import { AdminPage } from "../../../components/admin/admin-ui";
import { PolicyEditor } from "../../../components/admin/policy-editor";

export default function PolicyPage() {
  return (
    <AdminPage
      title="Call policy"
      description="Set platform-wide call duration and concurrency limits for new calls."
    >
      <PolicyEditor />
    </AdminPage>
  );
}
