import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import {
  WorkflowApiResponse,
  WorkflowParameter,
} from "@/routes/workflows/types/workflowTypes";
import { isConcreteWpid } from "./types";

function useTargetWorkflowParametersQuery(workflowPermanentId: string) {
  const credentialGetter = useCredentialGetter();
  const enabled = isConcreteWpid(workflowPermanentId);

  const { data, isLoading, isError } = useQuery<WorkflowApiResponse>({
    queryKey: ["workflow", "target", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/workflows/${workflowPermanentId}`)
        .then((r) => r.data);
    },
    enabled,
    staleTime: 5 * 60 * 1000,
  });

  const workflowParameters: Array<WorkflowParameter> =
    data?.workflow_definition.parameters.filter(
      (p): p is WorkflowParameter => p.parameter_type === "workflow",
    ) ?? [];

  return {
    workflowParameters,
    isLoading: enabled && isLoading,
    isError,
    workflowTitle: data?.title ?? "",
  };
}

export { useTargetWorkflowParametersQuery };
