import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { AxiosError } from "axios";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import type {
  CreateScheduleRequest,
  WorkflowScheduleResponse,
} from "@/routes/workflows/types/scheduleTypes";

function useCreateScheduleMutation() {
  const { workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (request: CreateScheduleRequest) => {
      if (!workflowPermanentId) {
        throw new Error("Missing workflowPermanentId");
      }
      const client = await getClient(credentialGetter);
      const response = await client.post<WorkflowScheduleResponse>(
        `/workflows/${workflowPermanentId}/schedules`,
        request,
      );
      return response.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workflowSchedules", workflowPermanentId],
      });
      toast({
        title: "Schedule created",
        variant: "success",
      });
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

function useToggleScheduleMutation() {
  const { workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      scheduleId,
      enabled,
    }: {
      scheduleId: string;
      enabled: boolean;
    }) => {
      if (!workflowPermanentId) {
        throw new Error("Missing workflowPermanentId");
      }
      const client = await getClient(credentialGetter);
      const action = enabled ? "enable" : "disable";
      const response = await client.post<WorkflowScheduleResponse>(
        `/workflows/${workflowPermanentId}/schedules/${scheduleId}/${action}`,
      );
      return response.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workflowSchedules", workflowPermanentId],
      });
      toast({
        title: "Schedule updated",
        variant: "success",
      });
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        title: "Failed to update schedule",
        description: detail || error.message,
        variant: "destructive",
      });
    },
  });
}

function useDeleteScheduleMutation() {
  const { workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (scheduleId: string) => {
      if (!workflowPermanentId) {
        throw new Error("Missing workflowPermanentId");
      }
      const client = await getClient(credentialGetter);
      await client.delete(
        `/workflows/${workflowPermanentId}/schedules/${scheduleId}`,
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workflowSchedules", workflowPermanentId],
      });
      toast({
        title: "Schedule deleted",
        variant: "success",
      });
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        title: "Failed to delete schedule",
        description: detail || error.message,
        variant: "destructive",
      });
    },
  });
}

export {
  useCreateScheduleMutation,
  useToggleScheduleMutation,
  useDeleteScheduleMutation,
};
