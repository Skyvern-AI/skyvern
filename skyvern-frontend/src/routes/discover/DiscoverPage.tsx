import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { PromptBox } from "../tasks/create/PromptBox";
import { WorkflowTemplates } from "./WorkflowTemplates";
import { useCreateWorkflowMutation } from "../workflows/hooks/useCreateWorkflowMutation";
import { Button } from "@/components/ui/button";
import { ReloadIcon } from "@radix-ui/react-icons";

const emptyWorkflowRequest = {
  title: "New Workflow",
  description: "",
  ai_fallback: true,
  code_version: 2 as const,
  run_with: "agent" as const,
  workflow_definition: {
    version: 2 as const,
    blocks: [],
    parameters: [],
  },
};

function DiscoverPage() {
  const enableCopilotHandoff =
    useFeatureFlag("ENABLE_DISCOVER_COPILOT_HANDOFF") === true;
  const createWorkflowMutation = useCreateWorkflowMutation();

  return (
    <div className="space-y-10">
      <div className="space-y-3">
        <PromptBox enableCopilotHandoff={enableCopilotHandoff} />
        <div className="flex justify-end">
          <Button
            variant="ghost"
            size="sm"
            className="text-slate-400 hover:text-slate-200"
            disabled={createWorkflowMutation.isPending}
            onClick={() =>
              createWorkflowMutation.mutate({
                ...emptyWorkflowRequest,
                _via: "blank",
              })
            }
          >
            {createWorkflowMutation.isPending && (
              <ReloadIcon className="mr-2 h-3 w-3 animate-spin" />
            )}
            Skip — start with blank canvas →
          </Button>
        </div>
      </div>
      <WorkflowTemplates />
    </div>
  );
}

export { DiscoverPage };
