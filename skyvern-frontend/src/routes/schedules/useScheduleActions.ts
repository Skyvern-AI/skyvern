import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import type {
  OrganizationScheduleItem,
  UpdateScheduleRequest,
  WorkflowScheduleResponse,
} from "@/routes/workflows/types/scheduleTypes";

function useEnableScheduleMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (schedule: OrganizationScheduleItem) => {
      const client = await getClient(credentialGetter);
      await client.post(
        `/workflows/${schedule.workflow_permanent_id}/schedules/${schedule.workflow_schedule_id}/enable`,
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["organizationSchedules"] });
      queryClient.invalidateQueries({ queryKey: ["scheduleDetail"] });
      toast({ title: "Schedule activated", variant: "success" });
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        title: "Failed to activate schedule",
        description: detail || error.message,
        variant: "destructive",
      });
    },
  });
}

function useDisableScheduleMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (schedule: OrganizationScheduleItem) => {
      const client = await getClient(credentialGetter);
      await client.post(
        `/workflows/${schedule.workflow_permanent_id}/schedules/${schedule.workflow_schedule_id}/disable`,
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["organizationSchedules"] });
      queryClient.invalidateQueries({ queryKey: ["scheduleDetail"] });
      toast({ title: "Schedule paused", variant: "success" });
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        title: "Failed to pause schedule",
        description: detail || error.message,
        variant: "destructive",
      });
    },
  });
}

function useDeleteOrgScheduleMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (schedule: OrganizationScheduleItem) => {
      const client = await getClient(credentialGetter);
      await client.delete(
        `/workflows/${schedule.workflow_permanent_id}/schedules/${schedule.workflow_schedule_id}`,
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["organizationSchedules"] });
      queryClient.invalidateQueries({ queryKey: ["scheduleDetail"] });
      toast({ title: "Schedule deleted", variant: "success" });
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

function useDuplicateScheduleMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (schedule: OrganizationScheduleItem) => {
      const client = await getClient(credentialGetter);
      await client.post(
        `/workflows/${schedule.workflow_permanent_id}/schedules`,
        {
          cron_expression: schedule.cron_expression,
          timezone: schedule.timezone,
          enabled: schedule.enabled,
          parameters: schedule.parameters,
          name: `${schedule.name ?? schedule.workflow_title} (copy)`,
        },
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["organizationSchedules"] });
      toast({ title: "Schedule duplicated", variant: "success" });
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        title: "Failed to duplicate schedule",
        description: detail || error.message,
        variant: "destructive",
      });
    },
  });
}

function useUpdateScheduleMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      workflowPermanentId,
      scheduleId,
      request,
    }: {
      workflowPermanentId: string;
      scheduleId: string;
      request: UpdateScheduleRequest;
    }) => {
      const client = await getClient(credentialGetter);
      const response = await client.put<WorkflowScheduleResponse>(
        `/workflows/${workflowPermanentId}/schedules/${scheduleId}`,
        request,
      );
      return response.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["organizationSchedules"] });
      queryClient.invalidateQueries({ queryKey: ["scheduleDetail"] });
      toast({ title: "Schedule updated", variant: "success" });
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

export {
  useEnableScheduleMutation,
  useDisableScheduleMutation,
  useDeleteOrgScheduleMutation,
  useDuplicateScheduleMutation,
  useUpdateScheduleMutation,
};
