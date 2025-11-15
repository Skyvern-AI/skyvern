import { getClient } from "@/api/AxiosClient";
import {
  Action,
  ActionApiResponse,
  ActionsApiResponse,
  ActionTypes,
  Status,
  StepApiResponse,
  TaskApiResponse,
} from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { statusIsNotFinalized } from "../../types";

function getActionInput(action: ActionApiResponse) {
  let input = "";
  if (action.action_type === ActionTypes.InputText && action.text) {
    input = action.text;
  } else if (action.action_type === ActionTypes.Click) {
    input = "Click";
  } else if (action.action_type === ActionTypes.Hover) {
    input = "Hover";
  } else if (action.action_type === ActionTypes.SelectOption && action.option) {
    input = action.option.label;
  }
  return input;
}

type Props = {
  id?: string;
};

function isOld(task: TaskApiResponse) {
  return new Date(task.created_at) < new Date(2024, 9, 21);
}

function useActions({ id }: Props): {
  data: Array<Action | null>;
  isLoading: boolean;
} {
  const credentialGetter = useCredentialGetter();

  const { data: task, isLoading: taskIsLoading } = useQuery<TaskApiResponse>({
    queryKey: ["task", id],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${id}`).then((response) => response.data);
    },
    refetchInterval: (query) => {
      if (!query.state.data) {
        return false;
      }
      if (statusIsNotFinalized(query.state.data)) {
        return 5000;
      }
      return false;
    },
    placeholderData: keepPreviousData,
  });

  const taskIsNotFinalized = task && statusIsNotFinalized(task);

  const { data: taskActions, isLoading: taskActionsIsLoading } = useQuery<
    Array<ActionsApiResponse>
  >({
    queryKey: ["tasks", id, "actions"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/tasks/${id}/actions`)
        .then((response) => response.data);
    },
    refetchInterval: taskIsNotFinalized ? 5000 : false,
    placeholderData: keepPreviousData,
    enabled: Boolean(task && !isOld(task)),
  });

  const { data: steps, isLoading: stepsIsLoading } = useQuery<
    Array<StepApiResponse>
  >({
    queryKey: ["task", id, "steps"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${id}/steps`).then((response) => response.data);
    },
    enabled: Boolean(task && isOld(task)),
    refetchOnWindowFocus: taskIsNotFinalized,
    refetchInterval: taskIsNotFinalized ? 5000 : false,
    placeholderData: keepPreviousData,
  });

  const actions =
    task && isOld(task)
      ? steps
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
                created_by: action.created_by,
              };
            });
            return actions;
          })
          .flat()
      : taskActions?.map((action) => {
          return {
            reasoning: action.reasoning ?? "",
            confidence: action.confidence_float ?? undefined,
            input: action.response ?? "",
            type: action.action_type,
            success:
              action.status === Status.Completed ||
              action.status === Status.Skipped,
            stepId: action.step_id ?? "",
            index: action.action_order ?? 0,
            created_by: action.created_by,
          };
        });

  return {
    data: actions ?? [],
    isLoading: taskIsLoading || taskActionsIsLoading || stepsIsLoading,
  };
}

export { useActions };
