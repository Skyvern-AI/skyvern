import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { PromptBox } from "../tasks/create/PromptBox";
import { WorkflowTemplates } from "./WorkflowTemplates";
import { Button } from "@/components/ui/button";
import { useNavigate } from "react-router-dom";
import { navigateToBlankAgentEditor } from "../workflows/blankAgentNavigation";

function DiscoverPage() {
  const enableCopilotHandoff =
    useFeatureFlag("ENABLE_DISCOVER_COPILOT_HANDOFF") === true;
  const navigate = useNavigate();

  return (
    <div className="space-y-10">
      <div className="space-y-3">
        <PromptBox enableCopilotHandoff={enableCopilotHandoff} />
        <div className="flex justify-end">
          <Button
            variant="ghost"
            size="sm"
            className="text-slate-400 hover:text-slate-200"
            onClick={() =>
              navigateToBlankAgentEditor(navigate, { via: "blank" })
            }
          >
            Skip — start with blank canvas →
          </Button>
        </div>
      </div>
      <WorkflowTemplates />
    </div>
  );
}

export { DiscoverPage };
