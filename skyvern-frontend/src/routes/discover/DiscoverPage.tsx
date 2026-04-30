import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { PromptBox } from "../tasks/create/PromptBox";
import { WorkflowTemplates } from "./WorkflowTemplates";

function DiscoverPage() {
  const enableCopilotHandoff =
    useFeatureFlag("ENABLE_DISCOVER_COPILOT_HANDOFF") === true;

  return (
    <div className="space-y-10">
      <PromptBox enableCopilotHandoff={enableCopilotHandoff} />
      <WorkflowTemplates />
    </div>
  );
}

export { DiscoverPage };
