import { useQueryClient, useMutation } from "@tanstack/react-query";
import { WorkflowCreateYAMLRequest } from "../types/workflowYamlTypes";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { useNavigate } from "react-router-dom";
import { stringify as convertToYAML } from "yaml";
import { WorkflowApiResponse } from "../types/workflowTypes";

type CreateWorkflowInput = WorkflowCreateYAMLRequest & { _via?: string };

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
  });
}

export { useCreateWorkflowMutation };
