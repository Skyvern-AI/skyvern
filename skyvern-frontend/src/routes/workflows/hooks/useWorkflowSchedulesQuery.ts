import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type {
  WorkflowSchedule,
  WorkflowScheduleListResponse,
} from "@/routes/workflows/types/scheduleTypes";

function useWorkflowSchedulesQuery() {
  const { workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();

  return useQuery<Array<WorkflowSchedule>>({
    queryKey: ["workflowSchedules", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get<WorkflowScheduleListResponse>(
        `/workflows/${workflowPermanentId}/schedules`,
      );
      return response.data.schedules;
    },
    enabled: !!workflowPermanentId,
    staleTime: 30_000,
  });
}

export { useWorkflowSchedulesQuery };
