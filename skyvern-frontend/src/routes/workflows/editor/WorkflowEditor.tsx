import { useParams } from "react-router-dom";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { getElements } from "./workflowEditorUtils";
import { useMutation } from "@tanstack/react-query";
import {
  BlockYAML,
  ParameterYAML,
  WorkflowCreateYAMLRequest,
} from "../types/workflowYamlTypes";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { stringify as convertToYAML } from "yaml";
import { ReactFlowProvider } from "@xyflow/react";
import { FlowRenderer } from "./FlowRenderer";
import { toast } from "@/components/ui/use-toast";
import { AxiosError } from "axios";

function WorkflowEditor() {
  const { workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();

  const { data: workflow, isLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  const saveWorkflowMutation = useMutation({
    mutationFn: async (data: {
      parameters: Array<ParameterYAML>;
      blocks: Array<BlockYAML>;
      title: string;
    }) => {
      if (!workflow || !workflowPermanentId) {
        return;
      }
      const client = await getClient(credentialGetter);
      const requestBody: WorkflowCreateYAMLRequest = {
        title: data.title,
        description: workflow.description,
        proxy_location: workflow.proxy_location,
        webhook_callback_url: workflow.webhook_callback_url,
        totp_verification_url: workflow.totp_verification_url,
        workflow_definition: {
          parameters: data.parameters,
          blocks: data.blocks,
        },
        is_saved_task: workflow.is_saved_task,
      };
      const yaml = convertToYAML(requestBody);
      return client
        .put(`/workflows/${workflowPermanentId}`, yaml, {
          headers: {
            "Content-Type": "text/plain",
          },
        })
        .then((response) => response.data);
    },
    onSuccess: () => {
      toast({
        title: "Changes saved",
        description: "Your changes have been saved",
        variant: "success",
      });
    },
    onError: (error: AxiosError) => {
      toast({
        title: "Error",
        description: error.message,
        variant: "destructive",
      });
    },
  });

  // TODO
  if (isLoading) {
    return (
      <div className="flex h-screen w-full items-center justify-center">
        Loading...
      </div>
    );
  }

  if (!workflow) {
    return null;
  }

  const elements = getElements(workflow.workflow_definition.blocks);

  return (
    <div className="h-screen w-full">
      <ReactFlowProvider>
        <FlowRenderer
          initialTitle={workflow.title}
          initialNodes={elements.nodes}
          initialEdges={elements.edges}
          initialParameters={workflow.workflow_definition.parameters
            .filter(
              (parameter) =>
                parameter.parameter_type === "workflow" ||
                parameter.parameter_type === "bitwarden_login_credential",
            )
            .map((parameter) => {
              if (parameter.parameter_type === "workflow") {
                return {
                  key: parameter.key,
                  parameterType: "workflow",
                  dataType: parameter.workflow_parameter_type,
                };
              } else {
                return {
                  key: parameter.key,
                  parameterType: "credential",
                  collectionId: parameter.bitwarden_collection_id,
                  urlParameterKey: parameter.url_parameter_key,
                };
              }
            })}
          handleSave={(parameters, blocks, title) => {
            saveWorkflowMutation.mutate({
              parameters,
              blocks,
              title,
            });
          }}
        />
      </ReactFlowProvider>
    </div>
  );
}

export { WorkflowEditor };
