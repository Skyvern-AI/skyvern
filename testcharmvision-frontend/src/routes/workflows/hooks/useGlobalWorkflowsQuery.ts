import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { WorkflowApiResponse } from "../types/workflowTypes";

function useGlobalWorkflowsQuery() {
  const credentialGetter = useCredentialGetter();
  return useQuery({
    queryKey: ["globalWorkflows"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.set("template", "true");
      params.set("page_size", "100");
      return client
        .get<Array<WorkflowApiResponse>>("/workflows", {
          params,
        })
        .then((response) => response.data);
    },
  });
}

export { useGlobalWorkflowsQuery };
