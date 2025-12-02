import { getClient } from "@/api/AxiosClient";
import { StepApiResponse } from "@/api/types";
import { cn } from "@/util/utils";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { PAGE_SIZE } from "../constants";
import { CheckboxIcon, CrossCircledIcon } from "@radix-ui/react-icons";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { apiPathPrefix } from "@/util/env";
import { useFirstParam } from "@/hooks/useFirstParam";

type Props = {
  activeIndex: number;
  onActiveIndexChange: (index: number) => void;
};

function StepNavigation({ activeIndex, onActiveIndexChange }: Props) {
  const taskId = useFirstParam("taskId", "runId");
  const [searchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const credentialGetter = useCredentialGetter();

  const {
    data: steps,
    isError,
    error,
  } = useQuery<Array<StepApiResponse>>({
    queryKey: ["task", taskId, "steps", page],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`${apiPathPrefix}/tasks/${taskId}/steps`, {
          params: {
            page,
            page_size: PAGE_SIZE,
          },
        })
        .then((response) => response.data);
    },
  });

  if (isError) {
    return <div>Error: {error?.message}</div>;
  }

  return (
    <nav className="flex flex-col gap-4">
      {steps?.map((step, index) => {
        const isActive = activeIndex === index;
        return (
          <div
            className={cn(
              "flex cursor-pointer items-center rounded-2xl px-6 py-2 hover:bg-primary-foreground",
              {
                "bg-primary-foreground": isActive,
              },
            )}
            key={step.step_id}
            onClick={() => {
              onActiveIndexChange(index);
            }}
          >
            {step.status === "completed" && (
              <CheckboxIcon className="mr-2 h-6 w-6 text-green-500" />
            )}
            {step.status === "failed" && (
              <CrossCircledIcon className="mr-2 h-6 w-6 text-red-500" />
            )}
            <span>
              {step.retry_index > 0
                ? `Step ${step.order + 1} ( Retry ${step.retry_index} )`
                : `Step ${step.order + 1}`}
            </span>
          </div>
        );
      })}
    </nav>
  );
}

export { StepNavigation };
