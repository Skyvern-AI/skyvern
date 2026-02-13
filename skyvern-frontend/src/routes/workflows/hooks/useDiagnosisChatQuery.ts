import { useQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type { DiagnosisChatHistoryResponse } from "@/api/types";

export function useDiagnosisChatHistoryQuery({
  workflowRunId,
}: {
  workflowRunId: string | null;
}) {
  const credentialGetter = useCredentialGetter();

  return useQuery<DiagnosisChatHistoryResponse>({
    queryKey: ["diagnosisChat", workflowRunId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get<DiagnosisChatHistoryResponse>(
        `/workflow_runs/${workflowRunId}/diagnosis/history`,
      );
      return response.data;
    },
    enabled: !!workflowRunId,
    staleTime: 1000 * 60 * 5, // 5 minutes
  });
}
