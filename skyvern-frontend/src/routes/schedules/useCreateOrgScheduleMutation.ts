import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import type {
  CreateScheduleRequest,
  WorkflowScheduleResponse,
} from "@/routes/workflows/types/scheduleTypes";

type CreateOrgScheduleParams = {
  workflowPermanentId: string;
  request: CreateScheduleRequest;
};

function useCreateOrgScheduleMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      workflowPermanentId,
      request,
    }: CreateOrgScheduleParams) => {
      const client = await getClient(credentialGetter);
      const response = await client.post<WorkflowScheduleResponse>(
        `/workflows/${workflowPermanentId}/schedules`,
        request,
      );
      return response.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["organizationSchedules"] });
      toast({ title: "Schedule created", variant: "success" });
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        title: "Failed to create schedule",
        description: detail || error.message,
        variant: "destructive",
      });
    },
  });
}

export { useCreateOrgScheduleMutation };
