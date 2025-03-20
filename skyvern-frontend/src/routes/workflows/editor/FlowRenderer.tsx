import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useShouldNotifyWhenClosingTab } from "@/hooks/useShouldNotifyWhenClosingTab";
import { DeleteNodeCallbackContext } from "@/store/DeleteNodeCallbackContext";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Background,
  BackgroundVariant,
  Controls,
  Edge,
  Panel,
  ReactFlow,
  useEdgesState,
  useNodesInitialized,
  useNodesState,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { AxiosError } from "axios";
import { nanoid } from "nanoid";
import { useEffect, useState } from "react";
import { useBlocker, useParams } from "react-router-dom";
import { stringify as convertToYAML } from "yaml";
import {
  AWSSecretParameter,
  WorkflowApiResponse,
  WorkflowEditorParameterTypes,
  WorkflowParameterTypes,
  WorkflowParameterValueType,
  WorkflowSettings,
} from "../types/workflowTypes";
import {
  BitwardenCreditCardDataParameterYAML,
  BitwardenLoginCredentialParameterYAML,
  BitwardenSensitiveInformationParameterYAML,
  BlockYAML,
  ContextParameterYAML,
  CredentialParameterYAML,
  ParameterYAML,
  WorkflowCreateYAMLRequest,
  WorkflowParameterYAML,
} from "../types/workflowYamlTypes";
import { WorkflowHeader } from "./WorkflowHeader";
import { WorkflowParametersStateContext } from "./WorkflowParametersStateContext";
import {
  BITWARDEN_CLIENT_ID_AWS_SECRET_KEY,
  BITWARDEN_CLIENT_SECRET_AWS_SECRET_KEY,
  BITWARDEN_MASTER_PASSWORD_AWS_SECRET_KEY,
} from "./constants";
import { edgeTypes } from "./edges";
import {
  AppNode,
  isWorkflowBlockNode,
  nodeTypes,
  WorkflowBlockNode,
} from "./nodes";
import { WorkflowNodeLibraryPanel } from "./panels/WorkflowNodeLibraryPanel";
import { WorkflowParametersPanel } from "./panels/WorkflowParametersPanel";
import "./reactFlowOverrideStyles.css";
import {
  convertEchoParameters,
  createNode,
  defaultEdge,
  descendants,
  generateNodeLabel,
  getAdditionalParametersForEmailBlock,
  getOutputParameterKey,
  getWorkflowBlocks,
  getWorkflowErrors,
  getWorkflowSettings,
  layout,
  nodeAdderNode,
  startNode,
} from "./workflowEditorUtils";
import { parameterIsBitwardenCredential, ParametersState } from "./types";

function convertToParametersYAML(
  parameters: ParametersState,
): Array<
  | WorkflowParameterYAML
  | BitwardenLoginCredentialParameterYAML
  | ContextParameterYAML
  | BitwardenSensitiveInformationParameterYAML
  | BitwardenCreditCardDataParameterYAML
  | CredentialParameterYAML
> {
  return parameters.map((parameter) => {
    if (parameter.parameterType === WorkflowEditorParameterTypes.Workflow) {
      return {
        parameter_type: WorkflowParameterTypes.Workflow,
        key: parameter.key,
        description: parameter.description || null,
        workflow_parameter_type: parameter.dataType,
        ...(parameter.defaultValue === null
          ? {}
          : { default_value: parameter.defaultValue }),
      };
    } else if (
      parameter.parameterType === WorkflowEditorParameterTypes.Context
    ) {
      return {
        parameter_type: WorkflowParameterTypes.Context,
        key: parameter.key,
        description: parameter.description || null,
        source_parameter_key: parameter.sourceParameterKey,
      };
    } else if (
      parameter.parameterType === WorkflowEditorParameterTypes.Secret
    ) {
      return {
        parameter_type: WorkflowParameterTypes.Bitwarden_Sensitive_Information,
        key: parameter.key,
        bitwarden_identity_key: parameter.identityKey,
        bitwarden_identity_fields: parameter.identityFields,
        description: parameter.description || null,
        bitwarden_collection_id: parameter.collectionId,
        bitwarden_client_id_aws_secret_key: BITWARDEN_CLIENT_ID_AWS_SECRET_KEY,
        bitwarden_client_secret_aws_secret_key:
          BITWARDEN_CLIENT_SECRET_AWS_SECRET_KEY,
        bitwarden_master_password_aws_secret_key:
          BITWARDEN_MASTER_PASSWORD_AWS_SECRET_KEY,
      };
    } else if (
      parameter.parameterType === WorkflowEditorParameterTypes.CreditCardData
    ) {
      return {
        parameter_type: WorkflowParameterTypes.Bitwarden_Credit_Card_Data,
        key: parameter.key,
        description: parameter.description || null,
        bitwarden_item_id: parameter.itemId,
        bitwarden_collection_id: parameter.collectionId,
        bitwarden_client_id_aws_secret_key: BITWARDEN_CLIENT_ID_AWS_SECRET_KEY,
        bitwarden_client_secret_aws_secret_key:
          BITWARDEN_CLIENT_SECRET_AWS_SECRET_KEY,
        bitwarden_master_password_aws_secret_key:
          BITWARDEN_MASTER_PASSWORD_AWS_SECRET_KEY,
      };
    } else {
      if (parameterIsBitwardenCredential(parameter)) {
        return {
          parameter_type: WorkflowParameterTypes.Bitwarden_Login_Credential,
          key: parameter.key,
          description: parameter.description || null,
          bitwarden_collection_id: parameter.collectionId,
          bitwarden_item_id: parameter.itemId,
          url_parameter_key: parameter.urlParameterKey,
          bitwarden_client_id_aws_secret_key:
            BITWARDEN_CLIENT_ID_AWS_SECRET_KEY,
          bitwarden_client_secret_aws_secret_key:
            BITWARDEN_CLIENT_SECRET_AWS_SECRET_KEY,
          bitwarden_master_password_aws_secret_key:
            BITWARDEN_MASTER_PASSWORD_AWS_SECRET_KEY,
        };
      } else {
        return {
          parameter_type: WorkflowParameterTypes.Workflow,
          workflow_parameter_type: WorkflowParameterValueType.CredentialId,
          default_value: parameter.credentialId,
          key: parameter.key,
          description: parameter.description || null,
        };
      }
    }
  });
}

type Props = {
  initialTitle: string;
  initialNodes: Array<AppNode>;
  initialEdges: Array<Edge>;
  initialParameters: ParametersState;
  workflow: WorkflowApiResponse;
};

export type AddNodeProps = {
  nodeType: NonNullable<WorkflowBlockNode["type"]>;
  previous: string | null;
  next: string | null;
  parent?: string;
  connectingEdgeType: string;
};

function FlowRenderer({
  initialTitle,
  initialEdges,
  initialNodes,
  initialParameters,
  workflow,
}: Props) {
  const { workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { workflowPanelState, setWorkflowPanelState, closeWorkflowPanel } =
    useWorkflowPanelStore();
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const [parameters, setParameters] = useState(initialParameters);
  const [title, setTitle] = useState(initialTitle);
  const nodesInitialized = useNodesInitialized();
  const { hasChanges, setHasChanges } = useWorkflowHasChangesStore();
  useShouldNotifyWhenClosingTab(hasChanges);
  const blocker = useBlocker(({ currentLocation, nextLocation }) => {
    return hasChanges && nextLocation.pathname !== currentLocation.pathname;
  });

  const saveWorkflowMutation = useMutation({
    mutationFn: async (data: {
      parameters: Array<ParameterYAML>;
      blocks: Array<BlockYAML>;
      title: string;
      settings: WorkflowSettings;
    }) => {
      if (!workflowPermanentId) {
        return;
      }
      const client = await getClient(credentialGetter);
      const requestBody: WorkflowCreateYAMLRequest = {
        title: data.title,
        description: workflow.description,
        proxy_location: data.settings.proxyLocation,
        webhook_callback_url: data.settings.webhookCallbackUrl,
        persist_browser_session: data.settings.persistBrowserSession,
        totp_verification_url: workflow.totp_verification_url,
        workflow_definition: {
          parameters: data.parameters,
          blocks: data.blocks,
        },
        is_saved_task: workflow.is_saved_task,
      };
      const yaml = convertToYAML(requestBody);
      return client.put<string, WorkflowApiResponse>(
        `/workflows/${workflowPermanentId}`,
        yaml,
        {
          headers: {
            "Content-Type": "text/plain",
          },
        },
      );
    },
    onSuccess: () => {
      toast({
        title: "Changes saved",
        description: "Your changes have been saved",
        variant: "success",
      });
      queryClient.invalidateQueries({
        queryKey: ["workflow", workflowPermanentId],
      });
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      setHasChanges(false);
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        title: "Error",
        description: detail ? detail : error.message,
        variant: "destructive",
      });
    },
  });

  function doLayout(nodes: Array<AppNode>, edges: Array<Edge>) {
    const layoutedElements = layout(nodes, edges);
    setNodes(layoutedElements.nodes);
    setEdges(layoutedElements.edges);
  }

  useEffect(() => {
    if (nodesInitialized) {
      doLayout(nodes, edges);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodesInitialized]);

  async function handleSave() {
    const blocks = getWorkflowBlocks(nodes, edges);
    const settings = getWorkflowSettings(nodes);
    const parametersInYAMLConvertibleJSON = convertToParametersYAML(parameters);
    const filteredParameters = workflow.workflow_definition.parameters.filter(
      (parameter) => {
        return parameter.parameter_type === "aws_secret";
      },
    ) as Array<AWSSecretParameter>;

    const echoParameters = convertEchoParameters(filteredParameters);

    const overallParameters = [
      ...parameters,
      ...echoParameters,
    ] as Array<ParameterYAML>;

    // if there is an email node, we need to add the email aws secret parameters
    const emailAwsSecretParameters = getAdditionalParametersForEmailBlock(
      blocks,
      overallParameters,
    );

    return saveWorkflowMutation.mutateAsync({
      parameters: [
        ...echoParameters,
        ...parametersInYAMLConvertibleJSON,
        ...emailAwsSecretParameters,
      ],
      blocks,
      title,
      settings,
    });
  }

  function addNode({
    nodeType,
    previous,
    next,
    parent,
    connectingEdgeType,
  }: AddNodeProps) {
    const newNodes: Array<AppNode> = [];
    const newEdges: Array<Edge> = [];
    const id = nanoid();
    const existingLabels = nodes
      .filter(isWorkflowBlockNode)
      .map((node) => node.data.label);
    const node = createNode(
      { id, parentId: parent },
      nodeType,
      generateNodeLabel(existingLabels),
    );
    newNodes.push(node);
    if (previous) {
      const newEdge = {
        id: nanoid(),
        type: "edgeWithAddButton",
        source: previous,
        target: id,
        style: {
          strokeWidth: 2,
        },
      };
      newEdges.push(newEdge);
    }
    if (next) {
      const newEdge = {
        id: nanoid(),
        type: connectingEdgeType,
        source: id,
        target: next,
        style: {
          strokeWidth: 2,
        },
      };
      newEdges.push(newEdge);
    }

    if (nodeType === "loop") {
      // when loop node is first created it needs an adder node so nodes can be added inside the loop
      const startNodeId = nanoid();
      const adderNodeId = nanoid();
      newNodes.push(
        startNode(
          startNodeId,
          {
            withWorkflowSettings: false,
            editable: true,
          },
          id,
        ),
      );
      newNodes.push(nodeAdderNode(adderNodeId, id));
      newEdges.push(defaultEdge(startNodeId, adderNodeId));
    }

    const editedEdges = previous
      ? edges.filter((edge) => edge.source !== previous)
      : edges;

    const previousNode = nodes.find((node) => node.id === previous);
    const previousNodeIndex = previousNode
      ? nodes.indexOf(previousNode)
      : nodes.length - 1;

    // creating some memory for no reason, maybe check it out later
    const newNodesAfter = [
      ...nodes.slice(0, previousNodeIndex + 1),
      ...newNodes,
      ...nodes.slice(previousNodeIndex + 1),
    ];

    setHasChanges(true);
    doLayout(newNodesAfter, [...editedEdges, ...newEdges]);
  }

  function deleteNode(id: string) {
    const node = nodes.find((node) => node.id === id);
    if (!node || !isWorkflowBlockNode(node)) {
      return;
    }
    const nodesToDelete = descendants(nodes, id);
    const deletedNodeLabel = node.data.label;
    const newNodes = nodes.filter(
      (node) => !nodesToDelete.includes(node) && node.id !== id,
    );
    const newEdges = edges.flatMap((edge) => {
      if (edge.source === id) {
        return [];
      }
      if (
        nodesToDelete.some(
          (node) => node.id === edge.source || node.id === edge.target,
        )
      ) {
        return [];
      }
      if (edge.target === id) {
        const nextEdge = edges.find((edge) => edge.source === id);
        if (nextEdge) {
          // connect the old incoming edge to the next node if both of them exist
          // also take the type of the old edge for plus button edge vs default
          return [
            {
              ...edge,
              type: nextEdge.type,
              target: nextEdge.target,
            },
          ];
        }
        return [edge];
      }
      return [edge];
    });

    if (newNodes.every((node) => node.type === "nodeAdder")) {
      // No user created nodes left, so return to the empty state.
      doLayout([], []);
      return;
    }

    // if any node was using the output parameter of the deleted node, remove it from their parameter keys
    const newNodesWithUpdatedParameters = newNodes.map((node) => {
      if (node.type === "task") {
        return {
          ...node,
          data: {
            ...node.data,
            parameterKeys: node.data.parameterKeys.filter(
              (parameter) =>
                parameter !== getOutputParameterKey(deletedNodeLabel),
            ),
          },
        };
      }
      // TODO: Fix this. When we put these into the same if statement TS fails to recognize that the returned value fits both the task and text prompt node types
      if (node.type === "textPrompt") {
        return {
          ...node,
          data: {
            ...node.data,
            parameterKeys: node.data.parameterKeys.filter(
              (parameter) =>
                parameter !== getOutputParameterKey(deletedNodeLabel),
            ),
          },
        };
      }
      if (node.type === "loop") {
        return {
          ...node,
          data: {
            ...node.data,
            loopValue:
              node.data.loopValue === getOutputParameterKey(deletedNodeLabel)
                ? ""
                : node.data.loopValue,
          },
        };
      }
      return node;
    });
    setHasChanges(true);
    doLayout(newNodesWithUpdatedParameters, newEdges);
  }

  return (
    <>
      <Dialog
        open={blocker.state === "blocked"}
        onOpenChange={(open) => {
          if (!open) {
            blocker.reset?.();
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Unsaved Changes</DialogTitle>
            <DialogDescription>
              Your workflow has unsaved changes. Do you want to save them before
              leaving?
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="secondary"
              onClick={() => {
                blocker.proceed?.();
              }}
            >
              Continue without saving
            </Button>
            <Button
              onClick={() => {
                handleSave().then(() => {
                  blocker.proceed?.();
                });
              }}
              disabled={saveWorkflowMutation.isPending}
            >
              {saveWorkflowMutation.isPending && (
                <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
              )}
              Save changes
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <WorkflowParametersStateContext.Provider
        value={[parameters, setParameters]}
      >
        <DeleteNodeCallbackContext.Provider value={deleteNode}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={(changes) => {
              const dimensionChanges = changes.filter(
                (change) => change.type === "dimensions",
              );
              const tempNodes = [...nodes];
              dimensionChanges.forEach((change) => {
                const node = tempNodes.find((node) => node.id === change.id);
                if (node) {
                  if (node.measured?.width) {
                    node.measured.width = change.dimensions?.width;
                  }
                  if (node.measured?.height) {
                    node.measured.height = change.dimensions?.height;
                  }
                }
              });
              if (dimensionChanges.length > 0) {
                doLayout(tempNodes, edges);
              }
              if (
                changes.some((change) => {
                  return (
                    change.type === "add" ||
                    change.type === "remove" ||
                    change.type === "replace"
                  );
                })
              ) {
                setHasChanges(true);
              }
              onNodesChange(changes);
            }}
            onEdgesChange={onEdgesChange}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            colorMode="dark"
            fitView
            fitViewOptions={{
              maxZoom: 1,
            }}
            deleteKeyCode={null}
          >
            <Background variant={BackgroundVariant.Dots} bgColor="#020617" />
            <Controls position="bottom-left" />
            <Panel position="top-center" className="h-20">
              <WorkflowHeader
                title={title}
                saving={saveWorkflowMutation.isPending}
                onTitleChange={(newTitle) => {
                  setTitle(newTitle);
                  setHasChanges(true);
                }}
                parametersPanelOpen={
                  workflowPanelState.active &&
                  workflowPanelState.content === "parameters"
                }
                onParametersClick={() => {
                  if (
                    workflowPanelState.active &&
                    workflowPanelState.content === "parameters"
                  ) {
                    closeWorkflowPanel();
                  } else {
                    setWorkflowPanelState({
                      active: true,
                      content: "parameters",
                    });
                  }
                }}
                onSave={async () => {
                  const errors = getWorkflowErrors(nodes);
                  if (errors.length > 0) {
                    toast({
                      title: "Can not save workflow because of errors:",
                      description: (
                        <div className="space-y-2">
                          {errors.map((error) => (
                            <p key={error}>{error}</p>
                          ))}
                        </div>
                      ),
                      variant: "destructive",
                    });
                    return;
                  }
                  await handleSave();
                }}
              />
            </Panel>
            {workflowPanelState.active && (
              <Panel position="top-right">
                {workflowPanelState.content === "parameters" && (
                  <WorkflowParametersPanel />
                )}
                {workflowPanelState.content === "nodeLibrary" && (
                  <WorkflowNodeLibraryPanel
                    onNodeClick={(props) => {
                      addNode(props);
                    }}
                  />
                )}
              </Panel>
            )}
          </ReactFlow>
        </DeleteNodeCallbackContext.Provider>
      </WorkflowParametersStateContext.Provider>
    </>
  );
}

export { FlowRenderer };
