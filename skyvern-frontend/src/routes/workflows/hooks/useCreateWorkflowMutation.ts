import { useQueryClient, useMutation } from "@tanstack/react-query";
import { WorkflowCreateYAMLRequest } from "../types/workflowYamlTypes";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { useNavigate } from "react-router-dom";
import { stringify as convertToYAML } from "yaml";
import { WorkflowApiResponse } from "../types/workflowTypes";
import { toast } from "@/components/ui/use-toast";
import axios from "axios";

type CreateWorkflowInput = WorkflowCreateYAMLRequest & { _via?: string };

function getCreateWorkflowErrorMessage(error: unknown) {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") {
      return detail;
    }
    return error.message;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return "Please try again.";
}

function useCreateWorkflowMutation() {
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: async (input: CreateWorkflowInput) => {
      const { _via: _, ...workflow } = input;
      void _;
      const client = await getClient(credentialGetter);
      const yaml = convertToYAML(workflow);
      return client.post<string, { data: WorkflowApiResponse }>(
        "/workflows",
        yaml,
        {
          headers: {
            "Content-Type": "text/plain",
          },
        },
      );
    },
    onSuccess: (response, variables) => {
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      queryClient.invalidateQueries({
        queryKey: ["folders"],
      });
      const via = variables._via;
      const search = via ? `?via=${encodeURIComponent(via)}` : "";
      navigate(
        `/workflows/${response.data.workflow_permanent_id}/build${search}`,
      );
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "Could not create agent",
        description: getCreateWorkflowErrorMessage(error),
      });
    },
  });
}

export { useCreateWorkflowMutation };
