import { useMountEffect } from "@/hooks/useMountEffect";
import { useSidebarStore } from "@/store/SidebarStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { ReactFlowProvider } from "@xyflow/react";
import { useParams } from "react-router-dom";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { FlowRenderer } from "./FlowRenderer";
import { getElements } from "./workflowEditorUtils";
import { LogoMinimized } from "@/components/LogoMinimized";
import {
  isDisplayedInWorkflowEditor,
  WorkflowEditorParameterTypes,
  WorkflowParameterTypes,
  WorkflowParameterValueType,
  WorkflowSettings,
} from "../types/workflowTypes";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";

function WorkflowEditor() {
  const { workflowPermanentId } = useParams();
  const setCollapsed = useSidebarStore((state) => {
    return state.setCollapsed;
  });
  const setHasChanges = useWorkflowHasChangesStore(
    (state) => state.setHasChanges,
  );

  const { data: workflow, isLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  const { data: globalWorkflows, isLoading: isGlobalWorkflowsLoading } =
    useGlobalWorkflowsQuery();

  useMountEffect(() => {
    setCollapsed(true);
    setHasChanges(false);
  });

  if (isLoading || isGlobalWorkflowsLoading) {
    return (
      <div className="flex h-screen w-full items-center justify-center">
        <LogoMinimized />
      </div>
    );
  }

  if (!workflow) {
    return null;
  }

  const isGlobalWorkflow = globalWorkflows?.some(
    (globalWorkflow) =>
      globalWorkflow.workflow_permanent_id === workflowPermanentId,
  );

  const settings: WorkflowSettings = {
    persistBrowserSession: workflow.persist_browser_session,
    proxyLocation: workflow.proxy_location,
    webhookCallbackUrl: workflow.webhook_callback_url,
  };

  const elements = getElements(
    workflow.workflow_definition.blocks,
    settings,
    !isGlobalWorkflow,
  );

  return (
    <div className="h-screen w-full">
      <ReactFlowProvider>
        <FlowRenderer
          initialTitle={workflow.title}
          initialNodes={elements.nodes}
          initialEdges={elements.edges}
          initialParameters={workflow.workflow_definition.parameters
            .filter((parameter) => isDisplayedInWorkflowEditor(parameter))
            .map((parameter) => {
              if (
                parameter.parameter_type === WorkflowParameterTypes.Workflow
              ) {
                if (
                  parameter.workflow_parameter_type ===
                  WorkflowParameterValueType.CredentialId
                ) {
                  return {
                    key: parameter.key,
                    parameterType: WorkflowEditorParameterTypes.Credential,
                    credentialId: parameter.default_value as string,
                    description: parameter.description,
                  };
                }
                return {
                  key: parameter.key,
                  parameterType: WorkflowEditorParameterTypes.Workflow,
                  dataType: parameter.workflow_parameter_type,
                  defaultValue: parameter.default_value,
                  description: parameter.description,
                };
              } else if (
                parameter.parameter_type === WorkflowParameterTypes.Context
              ) {
                return {
                  key: parameter.key,
                  parameterType: WorkflowEditorParameterTypes.Context,
                  sourceParameterKey: parameter.source.key,
                  description: parameter.description,
                };
              } else if (
                parameter.parameter_type ===
                WorkflowParameterTypes.Bitwarden_Sensitive_Information
              ) {
                return {
                  key: parameter.key,
                  parameterType: WorkflowEditorParameterTypes.Secret,
                  collectionId: parameter.bitwarden_collection_id,
                  identityKey: parameter.bitwarden_identity_key,
                  identityFields: parameter.bitwarden_identity_fields,
                  description: parameter.description,
                };
              } else if (
                parameter.parameter_type ===
                WorkflowParameterTypes.Bitwarden_Credit_Card_Data
              ) {
                return {
                  key: parameter.key,
                  parameterType: WorkflowEditorParameterTypes.CreditCardData,
                  collectionId: parameter.bitwarden_collection_id,
                  itemId: parameter.bitwarden_item_id,
                  description: parameter.description,
                };
              } else if (
                parameter.parameter_type === WorkflowParameterTypes.Credential
              ) {
                return {
                  key: parameter.key,
                  parameterType: WorkflowEditorParameterTypes.Credential,
                  credentialId: parameter.credential_id,
                  description: parameter.description,
                };
              } else {
                return {
                  key: parameter.key,
                  parameterType: WorkflowEditorParameterTypes.Credential,
                  collectionId: parameter.bitwarden_collection_id,
                  itemId: parameter.bitwarden_item_id,
                  urlParameterKey: parameter.url_parameter_key,
                  description: parameter.description,
                };
              }
            })}
          workflow={workflow}
        />
      </ReactFlowProvider>
    </div>
  );
}

export { WorkflowEditor };
