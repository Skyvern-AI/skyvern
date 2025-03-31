import { PromptBox } from "../tasks/create/PromptBox";
import { WorkflowTemplates } from "./WorkflowTemplates";

function DiscoverPage() {
  return (
    <div className="space-y-10">
      <PromptBox />
      <WorkflowTemplates />
    </div>
  );
}

export { DiscoverPage };
