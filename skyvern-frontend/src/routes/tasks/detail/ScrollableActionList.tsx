import { getClient } from "@/api/AxiosClient";
import { Action, ActionTypes, ReadableActionTypes } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  CheckCircledIcon,
  CrossCircledIcon,
  DotFilledIcon,
} from "@radix-ui/react-icons";
import { useQueryClient } from "@tanstack/react-query";
import { ReactNode, useEffect, useRef } from "react";
import { useParams } from "react-router-dom";

type Props = {
  data: Array<Action | null>;
  onNext: () => void;
  onPrevious: () => void;
  onActiveIndexChange: (index: number | "stream") => void;
  activeIndex: number | "stream";
  showStreamOption: boolean;
};

function ScrollableActionList({
  data,
  onNext,
  onPrevious,
  activeIndex,
  onActiveIndexChange,
  showStreamOption,
}: Props) {
  const { taskId } = useParams();
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const refs = useRef<Array<HTMLDivElement | null>>(
    Array.from({ length: data.length + 1 }),
  );

  useEffect(() => {
    if (typeof activeIndex === "number" && refs.current[activeIndex]) {
      refs.current[activeIndex]?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
    }
    if (activeIndex === "stream") {
      refs.current[data.length]?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
    }
  }, [activeIndex, data.length]);

  function getReverseActions() {
    const elements: ReactNode[] = [];
    for (let i = data.length - 1; i >= 0; i--) {
      const action = data[i];
      const actionIndex = data.length - i - 1;
      if (!action) {
        continue;
      }
      const selected = activeIndex === actionIndex;
      elements.push(
        <div
          key={i}
          ref={(element) => {
            refs.current[actionIndex] = element;
          }}
          className={cn(
            "flex p-4 rounded-lg shadow-md border hover:border-slate-300 cursor-pointer",
            {
              "border-slate-300": selected,
            },
          )}
          onClick={() => onActiveIndexChange(actionIndex)}
          onMouseEnter={() => {
            queryClient.prefetchQuery({
              queryKey: ["task", taskId, "steps", action.stepId, "artifacts"],
              queryFn: async () => {
                const client = await getClient(credentialGetter);
                return client
                  .get(`/tasks/${taskId}/steps/${action.stepId}/artifacts`)
                  .then((response) => response.data);
              },
            });
          }}
        >
          <div className="flex-1 p-2 pt-0 space-y-2">
            <div className="flex justify-between">
              <div className="flex gap-2 items-center">
                <span>#{i + 1}</span>
                <Badge>{ReadableActionTypes[action.type]}</Badge>
              </div>
              <div className="flex items-center gap-2">
                {typeof action.confidence === "number" && (
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger>
                        <Badge variant="secondary">{action.confidence}</Badge>
                      </TooltipTrigger>
                      <TooltipContent>Confidence Score</TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                )}
                {action.success ? (
                  <CheckCircledIcon className="w-6 h-6 text-success" />
                ) : (
                  <CrossCircledIcon className="w-6 h-6 text-destructive" />
                )}
              </div>
            </div>

            <div className="text-sm">{action.reasoning}</div>
            {action.type === ActionTypes.InputText && (
              <>
                <Separator className="bg-slate-50 block" />
                <div className="text-sm">Input: {action.input}</div>
              </>
            )}
          </div>
        </div>,
      );
    }
    return elements;
  }

  const actionIndex =
    typeof activeIndex === "number" ? data.length - activeIndex - 1 : "stream";

  return (
    <div className="w-1/3 flex flex-col items-center border rounded h-[40rem]">
      <div className="flex items-center text-sm p-4 gap-2">
        <Button
          size="icon"
          onClick={() => {
            onPrevious();
          }}
        >
          <ArrowUpIcon />
        </Button>
        {typeof actionIndex === "number" &&
          `#${actionIndex + 1} of ${data.length} total actions`}
        {activeIndex === "stream" && "Livestream"}
        <Button size="icon" onClick={() => onNext()}>
          <ArrowDownIcon />
        </Button>
      </div>
      <div className="overflow-y-scroll w-full px-4 pb-4 space-y-4">
        {showStreamOption && (
          <div
            key="stream"
            ref={(element) => {
              refs.current[data.length] = element;
            }}
            className={cn(
              "flex p-4 rounded-lg shadow-md border hover:border-slate-300 cursor-pointer",
              {
                "border-slate-300": activeIndex === "stream",
              },
            )}
            onClick={() => onActiveIndexChange("stream")}
          >
            <div className="text-lg flex gap-2 items-center">
              <DotFilledIcon className="w-6 h-6 text-red-500" />
              Live
            </div>
          </div>
        )}
        {getReverseActions()}
      </div>
    </div>
  );
}

export { ScrollableActionList };
