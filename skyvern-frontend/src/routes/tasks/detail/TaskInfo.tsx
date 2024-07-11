import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/StatusBadge";
import { Skeleton } from "@/components/ui/skeleton";
import { Status, StepApiResponse } from "@/api/types";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { useTaskQuery } from "./hooks/useTaskQuery";

type Props = {
  id: string;
};

const formatter = Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

function TaskInfo({ id }: Props) {
  const credentialGetter = useCredentialGetter();
  const {
    data: task,
    isLoading: taskIsLoading,
    isError: taskIsError,
  } = useTaskQuery({ id });

  const taskIsRunningOrQueued =
    task?.status === Status.Running || task?.status === Status.Queued;

  const {
    data: steps,
    isLoading: stepsIsLoading,
    isError: stepsIsError,
  } = useQuery<Array<StepApiResponse>>({
    queryKey: ["task", id, "steps"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${id}/steps`).then((response) => response.data);
    },
    refetchOnWindowFocus: taskIsRunningOrQueued,
    refetchInterval: taskIsRunningOrQueued ? 5000 : false,
    placeholderData: keepPreviousData,
  });

  if (stepsIsLoading || taskIsLoading) {
    return (
      <div className="flex gap-4">
        <Skeleton className="w-20 h-6" />
        <Skeleton className="w-20 h-6" />
        <Skeleton className="w-20 h-6" />
        <Skeleton className="w-20 h-6" />
      </div>
    );
  }

  if (stepsIsError || taskIsError) {
    return null;
  }

  const actionCount = steps?.reduce((acc, step) => {
    const actionsAndResults = step.output?.actions_and_results ?? [];

    const actionCount = actionsAndResults.reduce((acc, actionAndResult) => {
      const actionResult = actionAndResult[1];
      if (actionResult.length === 0) {
        return acc;
      }
      return acc + 1;
    }, 0);
    return acc + actionCount;
  }, 0);

  const notRunningSteps = steps?.filter((step) => step.status !== "running");
  const cost = notRunningSteps?.filter((step) => step.retry_index === 0).length;

  return (
    <div className="flex gap-4">
      {task && <StatusBadge status={task.status} />}
      <Badge>Steps: {notRunningSteps?.length}</Badge>
      <Badge>Actions: {actionCount}</Badge>
      <Badge>Cost: {cost && formatter.format(cost * 0.1)}</Badge>
    </div>
  );
}

export { TaskInfo };
