import { Action } from "@/api/types";
import { Button } from "@/components/ui/button";
import { ArrowLeftIcon, ArrowRightIcon } from "@radix-ui/react-icons";
import { useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { useEffect, useRef } from "react";
import { cn } from "@/util/utils";

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
    <div className="w-1/4 flex flex-col items-center border rounded h-[40rem]">
      <div className="flex items-center text-sm p-2 gap-2">
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
      <div className="overflow-y-scroll w-full p-4 space-y-4">
        {data.map((action, index) => {
          if (!action) {
            return null;
          }
          const selected = activeIndex === index;
          return (
            <div
              ref={(element) => {
                refs.current[index] = element;
              }}
              className={cn(
                "flex p-4 rounded-lg shadow-md border hover:bg-muted cursor-pointer",
                {
                  "bg-muted": selected,
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
              <div className="flex-1">
                <div className="text-sm">{`Action ${index + 1}`}</div>
                <div className="text-sm">{action.type}</div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export { ScrollableActionList };
