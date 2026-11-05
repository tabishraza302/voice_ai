import { AdminPage } from "../../../components/admin/admin-ui";
import { GlobalPromptEditor } from "../../../components/admin/global-prompt-editor";

export default function GlobalPromptPage() {
  return (
    <AdminPage
      title="Global prompt"
      description="Edit the shared instruction that guides every voice agent. Updates apply to new calls only."
    >
      <GlobalPromptEditor />
    </AdminPage>
  );
}
