import { useQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type { WorkflowScheduleResponse } from "@/routes/workflows/types/scheduleTypes";

function useScheduleDetailQuery(
  workflowPermanentId: string | undefined,
  scheduleId: string | undefined,
) {
  const credentialGetter = useCredentialGetter();

  return useQuery<WorkflowScheduleResponse>({
    queryKey: ["scheduleDetail", workflowPermanentId, scheduleId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get<WorkflowScheduleResponse>(
        `/workflows/${workflowPermanentId}/schedules/${scheduleId}`,
      );
      return response.data;
    },
    enabled: !!workflowPermanentId && !!scheduleId,
    staleTime: 30_000,
  });
}

export { useScheduleDetailQuery };
