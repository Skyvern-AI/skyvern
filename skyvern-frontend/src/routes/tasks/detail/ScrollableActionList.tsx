import { getClient } from "@/api/AxiosClient";
import { Action, ActionTypes } from "@/api/types";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";
import {
  CheckCircledIcon,
  CrossCircledIcon,
  DotFilledIcon,
  LightningBoltIcon,
} from "@radix-ui/react-icons";
import { useQueryClient } from "@tanstack/react-query";
import { ReactNode, useRef } from "react";
import { ActionTypePill } from "./ActionTypePill";

type Props = {
  data: Array<Action | null>;
  onActiveIndexChange: (index: number | "stream") => void;
  activeIndex: number | "stream";
  showStreamOption: boolean;
  taskDetails: {
    steps: number;
    actions: number;
    cost?: string;
  };
};

function ScrollableActionList({
  data,
  activeIndex,
  onActiveIndexChange,
  showStreamOption,
  taskDetails,
}: Props) {
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const refs = useRef<Array<HTMLDivElement | null>>(
    Array.from({ length: data.length + 1 }),
  );

  function getReverseActions() {
    const elements: ReactNode[] = [];
    for (let i = data.length - 1; i >= 0; i--) {
      const action = data[i];
      if (!action) {
        continue;
      }
      const selected = activeIndex === i;
      elements.push(
        <div
          key={i}
          ref={(element) => {
            refs.current[i] = element;
          }}
          className={cn(
            "flex cursor-pointer rounded-lg border-2 bg-slate-elevation3 hover:border-slate-50",
            {
              "border-l-destructive": !action.success,
              "border-l-success": action.success,
              "border-slate-50": selected,
            },
          )}
          onClick={() => onActiveIndexChange(i)}
          onMouseEnter={() => {
            queryClient.prefetchQuery({
              queryKey: ["step", action.stepId, "artifacts"],
              queryFn: async () => {
                const client = await getClient(credentialGetter);
                return client
                  .get(`/step/${action.stepId}/artifacts`)
                  .then((response) => response.data);
              },
            });
          }}
        >
          <div className="flex-1 space-y-2 p-4 pl-5">
            <div className="flex justify-between">
              <div className="flex items-center gap-2">
                <span>#{i + 1}</span>
              </div>
              <div className="flex items-center gap-2">
                <ActionTypePill actionType={action.type} />
                {action.created_by === "script" && (
                  <TooltipProvider>
                    <Tooltip delayDuration={300}>
                      <TooltipTrigger asChild>
                        <div className="flex gap-1 rounded-sm bg-slate-elevation5 px-2 py-1">
                          <LightningBoltIcon className="h-4 w-4 text-[gold]" />
                        </div>
                      </TooltipTrigger>
                      <TooltipContent className="max-w-[250px]">
                        Code Execution
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                )}
                {action.success ? (
                  <div className="flex gap-1 rounded-sm bg-slate-elevation5 px-2 py-1">
                    <CheckCircledIcon className="h-4 w-4 text-success" />
                  </div>
                ) : (
                  <div className="flex gap-1 rounded-sm bg-slate-elevation5 px-2 py-1">
                    <CrossCircledIcon className="h-4 w-4 text-destructive" />
                  </div>
                )}
              </div>
            </div>
            <div className="text-xs text-slate-400">{action.reasoning}</div>
            {action.type === ActionTypes.InputText && (
              <>
                <Separator />
                <div className="text-xs text-slate-400">
                  Input: {action.input}
                </div>
              </>
            )}
          </div>
        </div>,
      );
    }
    return elements;
  }

  return (
    <div className="h-[40rem] w-1/3 rounded border bg-slate-elevation1">
      <div className="grid grid-cols-2 gap-2 p-4">
        <div className="flex h-8 items-center justify-center rounded-sm bg-slate-700 px-3 text-xs text-gray-50">
          Actions: {taskDetails.actions}
        </div>
        <div className="flex h-8 items-center justify-center rounded-sm bg-slate-700 px-3 text-xs text-gray-50">
          Steps: {taskDetails.steps}
        </div>
      </div>
      <Separator />
      <ScrollArea className="p-4">
        <ScrollAreaViewport className="max-h-[34rem]">
          <div className="space-y-4">
            {showStreamOption && (
              <div
                key="stream"
                ref={(element) => {
                  refs.current[data.length] = element;
                }}
                className={cn(
                  "flex cursor-pointer rounded-lg border-2 bg-slate-elevation3 p-4 hover:border-slate-50",
                  {
                    "border-slate-50": activeIndex === "stream",
                  },
                )}
                onClick={() => onActiveIndexChange("stream")}
              >
                <div className="flex items-center gap-2">
                  <DotFilledIcon className="h-6 w-6 text-destructive" />
                  Live
                </div>
              </div>
            )}
            {getReverseActions()}
          </div>
        </ScrollAreaViewport>
      </ScrollArea>
    </div>
  );
}

export { ScrollableActionList };
