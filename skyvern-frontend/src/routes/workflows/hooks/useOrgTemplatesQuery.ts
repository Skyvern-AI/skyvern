import { useQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { WorkflowApiResponse } from "../types/workflowTypes";

function useOrgTemplatesQuery() {
  const credentialGetter = useCredentialGetter();

  return useQuery<WorkflowApiResponse[]>({
    queryKey: ["orgTemplates"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("only_templates", "true");
      params.append("page_size", "100");
      return client
        .get<WorkflowApiResponse[]>("/workflows", { params })
        .then((response) => response.data);
    },
  });
}

export { useOrgTemplatesQuery };
