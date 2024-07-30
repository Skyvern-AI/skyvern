import { getClient } from "@/api/AxiosClient";
import { Status, WorkflowRunApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

type Props = {
  workflowId: string;
};

type LastRunInfo = {
  status: Status | "N/A";
  time: string | "N/A";
};

function useWorkflowLastRunQuery({ workflowId }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryResult = useQuery<LastRunInfo | null>({
    queryKey: ["lastRunInfo", workflowId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const data = (await client
        .get(`/workflows/${workflowId}/runs?page_size=1`)
        .then((response) => response.data)) as Array<WorkflowRunApiResponse>;
      if (data.length === 0) {
        return {
          status: "N/A",
          time: "N/A",
        };
      }
      return {
        status: data[0]!.status,
        time: data[0]!.created_at,
      };
    },
  });

  return queryResult;
}

export { useWorkflowLastRunQuery };
