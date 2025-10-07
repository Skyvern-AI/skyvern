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
import { useDebugStore } from "@/store/useDebugStore";
import { cn } from "@/util/utils";

interface Props {
  blockLabel: string;
}

function NodeTabs({ blockLabel }: Props) {
  const { blockLabel: urlBlockLabel } = useParams();
  const blockOutput = useBlockOutputStore((state) => state.outputs[blockLabel]);
  const debugStore = useDebugStore();
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

  if (!debugStore.isDebugMode) {
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
      <div
        className={cn(
          "absolute right-[-1rem] top-0 h-[6rem] w-[2rem] overflow-visible",
          { "top-[2.5rem]": thisBlockIsTargetted },
        )}
      >
        <div className="relative flex h-full w-full items-start justify-center gap-1 overflow-visible">
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <div
                  className={cn(
                    "flex h-[2.5rem] w-[2.5rem] min-w-[2.5rem] rotate-[-90deg] items-center justify-center gap-2 rounded-[50%] bg-slate-elevation3 p-2",
                    {
                      "opacity-100 outline outline-2 outline-slate-300":
                        thisBlockIsTargetted,
                    },
                    {
                      "hover:translate-x-[1px] active:translate-x-[0px]":
                        blockOutput,
                    },
                  )}
                >
                  <Button
                    variant="link"
                    size="sm"
                    disabled={!blockOutput}
                    className={cn("p-0 opacity-80 hover:opacity-100", {
                      "opacity-100": isExpanded,
                    })}
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
                {!blockOutput
                  ? "No outputs. Run block first."
                  : isExpanded
                    ? "Close Outputs"
                    : "Open Outputs"}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      </div>
    </>
  );
}

export { NodeTabs };
