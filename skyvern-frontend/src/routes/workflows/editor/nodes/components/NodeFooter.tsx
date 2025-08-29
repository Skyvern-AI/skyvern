import { useState } from "react";
import { useParams } from "react-router-dom";
import { CrossCircledIcon } from "@radix-ui/react-icons";
import { OutputIcon } from "@/components/icons/OutputIcon";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { BlockOutputs } from "@/routes/workflows/components/BlockOutputs";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useBlockOutputStore } from "@/store/BlockOutputStore";
import { cn } from "@/util/utils";

interface Props {
  blockLabel: string;
}

function NodeFooter({ blockLabel }: Props) {
  const { blockLabel: urlBlockLabel } = useParams();
  const blockOutput = useBlockOutputStore((state) => state.outputs[blockLabel]);
  const [isExpanded, setIsExpanded] = useState(false);
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued &&
    urlBlockLabel !== undefined &&
    urlBlockLabel === blockLabel;
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === blockLabel;

  if (thisBlockIsPlaying) {
    return null;
  }

  return (
    <>
      <div
        className={cn(
          "pointer-events-none absolute left-0 top-[-1rem] h-full w-full",
          { "opacity-100": isExpanded },
        )}
      >
        <div className="relative h-full w-full overflow-hidden rounded-lg">
          <div
            className={cn(
              "pointer-events-auto flex h-full w-full translate-y-full items-center justify-center bg-slate-elevation3 p-6 transition-all duration-300 ease-in-out",
              { "translate-y-0": isExpanded },
            )}
          >
            <BlockOutputs
              blockLabel={blockLabel}
              blockOutput={
                blockOutput ? JSON.parse(JSON.stringify(blockOutput)) : null
              }
            />
          </div>
        </div>
      </div>
      <div className="relative flex w-full overflow-visible bg-[pink]">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <div
                className={cn(
                  "absolute bottom-[-2.25rem] right-[-0.75rem] flex h-[2.5rem] w-[2.5rem] items-center justify-center gap-2 rounded-[50%] bg-slate-elevation3 p-2",
                  {
                    "opacity-100 outline outline-2 outline-slate-300":
                      thisBlockIsTargetted,
                  },
                )}
              >
                <Button
                  variant="link"
                  size="sm"
                  className={cn(
                    "p-0 opacity-80 hover:translate-y-[-1px] hover:opacity-100 active:translate-y-[0px]",
                    { "opacity-100": isExpanded },
                  )}
                  onClick={() => {
                    setIsExpanded(!isExpanded);
                  }}
                >
                  {isExpanded ? (
                    <CrossCircledIcon className="scale-[110%]" />
                  ) : (
                    <OutputIcon className="scale-[80%]" />
                  )}
                </Button>
              </div>
            </TooltipTrigger>
            <TooltipContent>
              {isExpanded ? "Close Outputs" : "Open Outputs"}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      </div>
    </>
  );
}

export { NodeFooter };
