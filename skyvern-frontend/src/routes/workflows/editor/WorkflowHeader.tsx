import { SaveIcon } from "@/components/icons/SaveIcon";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  ChevronDownIcon,
  ChevronUpIcon,
  CopyIcon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useNavigate, useParams } from "react-router-dom";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { EditableNodeTitle } from "./nodes/components/EditableNodeTitle";
import { useCreateWorkflowMutation } from "../hooks/useCreateWorkflowMutation";
import { convert } from "./workflowEditorUtils";

type Props = {
  title: string;
  parametersPanelOpen: boolean;
  onParametersClick: () => void;
  onSave: () => void;
  onTitleChange: (title: string) => void;
  saving: boolean;
};

function WorkflowHeader({
  title,
  parametersPanelOpen,
  onParametersClick,
  onSave,
  onTitleChange,
  saving,
}: Props) {
  const { workflowPermanentId } = useParams();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const navigate = useNavigate();
  const createWorkflowMutation = useCreateWorkflowMutation();

  if (!globalWorkflows) {
    return null; // this should be loaded already by some other components
  }

  const isGlobalWorkflow = globalWorkflows.some(
    (workflow) => workflow.workflow_permanent_id === workflowPermanentId,
  );

  return (
    <div className="flex h-full w-full justify-between rounded-xl bg-slate-elevation2 px-6 py-5">
      <div className="flex h-full items-center">
        <EditableNodeTitle
          editable={true}
          onChange={onTitleChange}
          value={title}
          titleClassName="text-3xl"
          inputClassName="text-3xl"
        />
      </div>
      <div className="flex h-full items-center justify-end gap-4">
        {isGlobalWorkflow ? (
          <Button
            size="lg"
            onClick={() => {
              const workflow = globalWorkflows.find(
                (workflow) =>
                  workflow.workflow_permanent_id === workflowPermanentId,
              );
              if (!workflow) {
                return; // makes no sense
              }
              const clone = convert(workflow);
              createWorkflowMutation.mutate(clone);
            }}
          >
            {createWorkflowMutation.isPending ? (
              <ReloadIcon className="mr-3 h-6 w-6 animate-spin" />
            ) : (
              <CopyIcon className="mr-3 h-6 w-6" />
            )}
            Make a Copy to Edit
          </Button>
        ) : (
          <>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    size="icon"
                    variant="tertiary"
                    className="size-10"
                    disabled={isGlobalWorkflow}
                    onClick={() => {
                      onSave();
                    }}
                  >
                    {saving ? (
                      <ReloadIcon className="size-6 animate-spin" />
                    ) : (
                      <SaveIcon className="size-6" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Save</TooltipContent>
              </Tooltip>
            </TooltipProvider>
            <Button variant="tertiary" size="lg" onClick={onParametersClick}>
              <span className="mr-2">Parameters</span>
              {parametersPanelOpen ? (
                <ChevronUpIcon className="h-6 w-6" />
              ) : (
                <ChevronDownIcon className="h-6 w-6" />
              )}
            </Button>
            <Button
              size="lg"
              onClick={() => {
                navigate(`/workflows/${workflowPermanentId}/run`);
              }}
            >
              <PlayIcon className="mr-2 h-6 w-6" />
              Run
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

export { WorkflowHeader };
