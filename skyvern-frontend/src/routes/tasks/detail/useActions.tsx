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

function useActions(taskId: string): {
  data?: Array<Action>;
  isFetching: boolean;
} {
  const credentialGetter = useCredentialGetter();
  const { data: task, isFetching: taskIsFetching } = useQuery<TaskApiResponse>({
    queryKey: ["task", taskId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${taskId}`).then((response) => response.data);
    },
  });

  const taskIsRunningOrQueued =
    task?.status === Status.Running || task?.status === Status.Queued;

  const stepsQuery = useQuery<Array<StepApiResponse>>({
    queryKey: ["task", taskId, "steps"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/tasks/${taskId}/steps`)
        .then((response) => response.data);
    },
    enabled: !!task,
    staleTime: taskIsRunningOrQueued ? 30 : Infinity,
    refetchOnWindowFocus: taskIsRunningOrQueued,
  });

  const actions = stepsQuery.data
    ?.map((step) => {
      const actionsAndResults = step.output?.actions_and_results ?? [];

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
    })
    .flat();

  return {
    data: actions,
    isFetching: stepsQuery.isFetching || taskIsFetching,
  };
}

export { useActions };
