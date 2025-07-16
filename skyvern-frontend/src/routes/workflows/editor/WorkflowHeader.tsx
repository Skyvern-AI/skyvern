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
  Crosshair1Icon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useNavigate, useParams } from "react-router-dom";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { EditableNodeTitle } from "./nodes/components/EditableNodeTitle";
import { useCreateWorkflowMutation } from "../hooks/useCreateWorkflowMutation";
import { convert } from "./workflowEditorUtils";
import { useDebugStore } from "@/store/useDebugStore";
import { cn } from "@/util/utils";

type Props = {
  debuggableBlockCount: number;
  title: string;
  parametersPanelOpen: boolean;
  onParametersClick: () => void;
  onSave: () => void;
  onTitleChange: (title: string) => void;
  saving: boolean;
};

function WorkflowHeader({
  debuggableBlockCount,
  title,
  parametersPanelOpen,
  onParametersClick,
  onSave,
  onTitleChange,
  saving,
}: Props) {
  const { blockLabel: urlBlockLabel, workflowPermanentId } = useParams();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const navigate = useNavigate();
  const createWorkflowMutation = useCreateWorkflowMutation();
  const debugStore = useDebugStore();
  const anyBlockIsPlaying =
    urlBlockLabel !== undefined && urlBlockLabel.length > 0;

  if (!globalWorkflows) {
    return null; // this should be loaded already by some other components
  }

  const isGlobalWorkflow = globalWorkflows.some(
    (workflow) => workflow.workflow_permanent_id === workflowPermanentId,
  );

  return (
    <div
      className={cn(
        "flex h-full w-full justify-between rounded-xl bg-slate-elevation2 px-6 py-5",
      )}
    >
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
            <Button
              size="lg"
              variant={debugStore.isDebugMode ? "default" : "tertiary"}
              disabled={debuggableBlockCount === 0 || anyBlockIsPlaying}
              onClick={() => {
                if (debugStore.isDebugMode) {
                  navigate(`/workflows/${workflowPermanentId}/edit`);
                } else {
                  navigate(`/workflows/${workflowPermanentId}/debug`);
                }
              }}
            >
              <Crosshair1Icon className="mr-2 h-6 w-6" />
              {debugStore.isDebugMode ? "End Debugging" : "Start Debugging"}
            </Button>
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
            {!debugStore.isDebugMode && (
              <Button
                size="lg"
                onClick={() => {
                  navigate(`/workflows/${workflowPermanentId}/run`);
                }}
              >
                <PlayIcon className="mr-2 h-6 w-6" />
                Run
              </Button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export { WorkflowHeader };
