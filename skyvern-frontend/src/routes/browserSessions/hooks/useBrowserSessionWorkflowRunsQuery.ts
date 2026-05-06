import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { WorkflowRunApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

type Props = {
  browserSessionId: string | undefined;
  page: number;
  pageSize?: number;
};

function useBrowserSessionWorkflowRunsQuery({
  browserSessionId,
  page,
  pageSize,
}: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<Array<WorkflowRunApiResponse>>({
    queryKey: ["browserSessionWorkflowRuns", browserSessionId, page, pageSize],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const params = new URLSearchParams();
      params.append("page", String(page));
      if (pageSize) {
        params.append("page_size", String(pageSize));
      }
      const response = await client.get<Array<WorkflowRunApiResponse>>(
        `/browser_sessions/${browserSessionId}/workflow_runs`,
        { params },
      );
      return response.data;
    },
    enabled: !!browserSessionId,
  });
}

export { useBrowserSessionWorkflowRunsQuery };
