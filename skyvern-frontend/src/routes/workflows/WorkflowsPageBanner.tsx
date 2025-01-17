import { Button } from "@/components/ui/button";
import { ImportWorkflowButton } from "./ImportWorkflowButton";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { WorkflowCreateYAMLRequest } from "./types/workflowYamlTypes";
import { stringify as convertToYAML } from "yaml";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { PlusIcon, ReloadIcon } from "@radix-ui/react-icons";

const emptyWorkflowRequest: WorkflowCreateYAMLRequest = {
  title: "New Workflow",
  description: "",
  workflow_definition: {
    blocks: [],
    parameters: [],
  },
};

function WorkflowsPageBanner() {
  const navigate = useNavigate();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  const createNewWorkflowMutation = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter);
      const yaml = convertToYAML(emptyWorkflowRequest);
      return client.post<
        typeof emptyWorkflowRequest,
        { data: WorkflowApiResponse }
      >("/workflows", yaml, {
        headers: {
          "Content-Type": "text/plain",
        },
      });
    },
    onSuccess: (response) => {
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      navigate(`/workflows/${response.data.workflow_permanent_id}/edit`);
    },
  });

  return (
    <div className="space-y-8 bg-slate-elevation1 p-12">
      <div className="flex justify-center text-3xl font-bold">
        <h1>Workflows</h1>
      </div>
      <div className="flex justify-center gap-4">
        <ImportWorkflowButton />
        <Button
          disabled={createNewWorkflowMutation.isPending}
          onClick={() => {
            createNewWorkflowMutation.mutate();
          }}
        >
          {createNewWorkflowMutation.isPending ? (
            <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <PlusIcon className="mr-2 h-4 w-4" />
          )}
          Create Workflow
        </Button>
      </div>
      <div className="flex">
        <div className="mx-auto flex flex-col gap-3">
          <div className="font-bold">
            Workflows let you create complex web-agents that can:
          </div>
          <div className="flex gap-2">
            <div className="flex size-6 items-center justify-center rounded-full bg-primary text-primary-foreground">
              1
            </div>
            <div>Save browser sessions and re-use them in subsequent runs</div>
          </div>
          <div className="flex gap-2">
            <div className="flex size-6 items-center justify-center rounded-full bg-primary text-primary-foreground">
              2
            </div>
            <div>
              Connect multiple agents together to carry out complex objectives
            </div>
          </div>
          <div className="flex gap-2">
            <div className="flex size-6 items-center justify-center rounded-full bg-primary text-primary-foreground">
              3
            </div>
            <div>
              Allow Skyvern agents to execute non-browser tasks such as sending
              emails, or parsing PDFs
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export { WorkflowsPageBanner };
