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
  ArrowLeftIcon,
  ArrowRightIcon,
  CheckCircledIcon,
  CrossCircledIcon,
} from "@radix-ui/react-icons";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { useParams } from "react-router-dom";

type Props = {
  data: Array<Action | null>;
  onNext: () => void;
  onPrevious: () => void;
  onActiveIndexChange: (index: number) => void;
  activeIndex: number;
};

function ScrollableActionList({
  data,
  onNext,
  onPrevious,
  activeIndex,
  onActiveIndexChange,
}: Props) {
  const { taskId } = useParams();
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const refs = useRef<Array<HTMLDivElement | null>>(
    Array.from({ length: data.length }),
  );

  useEffect(() => {
    if (refs.current[activeIndex]) {
      refs.current[activeIndex]?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
    }
  }, [activeIndex]);

  return (
    <div className="w-1/3 flex flex-col items-center border rounded h-[40rem]">
      <div className="flex items-center text-sm p-4 gap-2">
        <Button
          size="icon"
          onClick={() => {
            onPrevious();
          }}
        >
          <ArrowLeftIcon />
        </Button>
        {activeIndex + 1} of {data.length} total actions
        <Button size="icon" onClick={() => onNext()}>
          <ArrowRightIcon />
        </Button>
      </div>
      <div className="overflow-y-scroll w-full px-4 pb-4 space-y-4">
        {data.map((action, index) => {
          if (!action) {
            return null;
          }
          const selected = activeIndex === index;
          return (
            <div
              key={index}
              ref={(element) => {
                refs.current[index] = element;
              }}
              className={cn(
                "flex p-4 rounded-lg shadow-md border hover:border-slate-500 cursor-pointer",
                {
                  "border-slate-500": selected,
                },
              )}
              onClick={() => onActiveIndexChange(index)}
              onMouseEnter={() => {
                queryClient.prefetchQuery({
                  queryKey: [
                    "task",
                    taskId,
                    "steps",
                    action.stepId,
                    "artifacts",
                  ],
                  queryFn: async () => {
                    const client = await getClient(credentialGetter);
                    return client
                      .get(`/tasks/${taskId}/steps/${action.stepId}/artifacts`)
                      .then((response) => response.data);
                  },
                  staleTime: Infinity,
                });
              }}
            >
              <div className="flex-1 p-2 pt-0 space-y-2">
                <div className="flex justify-between">
                  <div className="flex gap-2 items-center">
                    <span>#{index + 1}</span>
                    <Badge>{ReadableActionTypes[action.type]}</Badge>
                  </div>
                  <div className="flex items-center gap-2">
                    {typeof action.confidence === "number" && (
                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger>
                            <Badge variant="secondary">
                              {action.confidence}
                            </Badge>
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
            </div>
          );
        })}
      </div>
    </div>
  );
}

export { ScrollableActionList };
