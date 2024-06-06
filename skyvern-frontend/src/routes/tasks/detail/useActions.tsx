import { getClient } from "@/api/AxiosClient";
import {
  Action,
  ActionApiResponse,
  ActionTypes,
  Status,
  StepApiResponse,
  TaskApiResponse,
} from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

function getActionInput(action: ActionApiResponse) {
  let input = "";
  if (action.action_type === ActionTypes.InputText && action.text) {
    input = action.text;
  } else if (action.action_type === ActionTypes.Click) {
    input = "Click";
  } else if (action.action_type === ActionTypes.SelectOption && action.option) {
    input = action.option.label;
  }
  return input;
}

function useActions(
  taskId: string,
): ReturnType<typeof useQuery<Array<Action | null>>> {
  const credentialGetter = useCredentialGetter();
  const { data: task } = useQuery<TaskApiResponse>({
    queryKey: ["task", taskId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${taskId}`).then((response) => response.data);
    },
  });

  const taskIsRunningOrQueued =
    task?.status === Status.Running || task?.status === Status.Queued;

  const useQueryReturn = useQuery<Array<Action | null>>({
    queryKey: ["task", taskId, "actions"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const steps = (await client
        .get(`/tasks/${taskId}/steps`)
        .then((response) => response.data)) as Array<StepApiResponse>;

      const actions = steps.map((step) => {
        const actionsAndResults = step.output.actions_and_results;

        const actions = actionsAndResults.map((actionAndResult, index) => {
          const action: Action = {
            reasoning: actionAndResult[0].reasoning,
            confidence: actionAndResult[0].confidence_float,
            input: getActionInput(actionAndResult[0]),
            type: actionAndResult[0].action_type,
            success: actionAndResult[1][0].success,
            stepId: step.step_id,
            index,
          };
          return action;
        });
        return actions;
      });

      return actions.flat();
    },
    enabled: !!task,
    staleTime: taskIsRunningOrQueued ? 30 : Infinity,
    refetchOnWindowFocus: taskIsRunningOrQueued,
  });

  return useQueryReturn;
}

export { useActions };
