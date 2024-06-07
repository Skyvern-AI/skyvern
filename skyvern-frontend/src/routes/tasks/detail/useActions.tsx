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
  data?: Array<Action | null>;
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
        const action = actionAndResult[0];
        const actionResult = actionAndResult[1];
        if (actionResult.length === 0) {
          return null;
        }
        return {
          reasoning: action.reasoning,
          confidence: action.confidence_float,
          input: getActionInput(action),
          type: action.action_type,
          success: actionResult?.[0]?.success ?? false,
          stepId: step.step_id,
          index,
        };
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
