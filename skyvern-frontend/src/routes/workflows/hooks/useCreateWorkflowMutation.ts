import { useQueryClient } from "@tanstack/react-query";
import { useMutation } from "@tanstack/react-query";
import { WorkflowCreateYAMLRequest } from "../types/workflowYamlTypes";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { useNavigate } from "react-router-dom";
import { stringify as convertToYAML } from "yaml";
import { WorkflowApiResponse } from "../types/workflowTypes";

function useCreateWorkflowMutation() {
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: async (workflow: WorkflowCreateYAMLRequest) => {
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
    onSuccess: (response) => {
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      queryClient.invalidateQueries({
        queryKey: ["folders"],
      });
      navigate(`/workflows/${response.data.workflow_permanent_id}/debug`);
    },
  });
}

export { useCreateWorkflowMutation };
