import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useOnChange } from "@/hooks/useOnChange";
import { useShouldNotifyWhenClosingTab } from "@/hooks/useShouldNotifyWhenClosingTab";
import { BlockActionContext } from "@/store/BlockActionContext";
import { useDebugStore } from "@/store/useDebugStore";
import {
  useWorkflowHasChangesStore,
  useWorkflowSave,
  type WorkflowSaveData,
} from "@/store/WorkflowHasChangesStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { ReloadIcon } from "@radix-ui/react-icons";
import {
  Background,
  BackgroundVariant,
  Controls,
  Edge,
  PanOnScrollMode,
  ReactFlow,
  Viewport,
  useNodesInitialized,
  useReactFlow,
  NodeChange,
  EdgeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useRef, useState } from "react";
import { useBlocker } from "react-router-dom";
import {
  AWSSecretParameter,
  debuggableWorkflowBlockTypes,
  WorkflowApiResponse,
  WorkflowEditorParameterTypes,
  WorkflowParameterTypes,
  WorkflowParameterValueType,
} from "../types/workflowTypes";
import {
  BitwardenCreditCardDataParameterYAML,
  BitwardenLoginCredentialParameterYAML,
  BitwardenSensitiveInformationParameterYAML,
  ContextParameterYAML,
  CredentialParameterYAML,
  OnePasswordCredentialParameterYAML,
  ParameterYAML,
  WorkflowParameterYAML,
} from "../types/workflowYamlTypes";
import {
  BITWARDEN_CLIENT_ID_AWS_SECRET_KEY,
  BITWARDEN_CLIENT_SECRET_AWS_SECRET_KEY,
  BITWARDEN_MASTER_PASSWORD_AWS_SECRET_KEY,
} from "./constants";
import { edgeTypes } from "./edges";
import { AppNode, isWorkflowBlockNode, nodeTypes } from "./nodes";
import {
  ParametersState,
  parameterIsSkyvernCredential,
  parameterIsOnePasswordCredential,
  parameterIsBitwardenCredential,
} from "./types";
import "./reactFlowOverrideStyles.css";
import {
  convertEchoParameters,
  descendants,
  getAdditionalParametersForEmailBlock,
  getOrderedChildrenBlocks,
  getOutputParameterKey,
  getWorkflowBlocks,
  getWorkflowSettings,
  layout,
} from "./workflowEditorUtils";
import { useAutoPan } from "./useAutoPan";

function convertToParametersYAML(
  parameters: ParametersState,
): Array<
  | WorkflowParameterYAML
  | BitwardenLoginCredentialParameterYAML
  | ContextParameterYAML
  | BitwardenSensitiveInformationParameterYAML
  | BitwardenCreditCardDataParameterYAML
  | OnePasswordCredentialParameterYAML
  | CredentialParameterYAML
> {
  return parameters
    .map(
      (
        parameter: ParametersState[number],
      ):
        | WorkflowParameterYAML
        | BitwardenLoginCredentialParameterYAML
        | ContextParameterYAML
        | BitwardenSensitiveInformationParameterYAML
        | BitwardenCreditCardDataParameterYAML
        | OnePasswordCredentialParameterYAML
        | CredentialParameterYAML
        | undefined => {
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
            parameter_type:
              WorkflowParameterTypes.Bitwarden_Sensitive_Information,
            key: parameter.key,
            bitwarden_identity_key: parameter.identityKey,
            bitwarden_identity_fields: parameter.identityFields,
            description: parameter.description || null,
            bitwarden_collection_id: parameter.collectionId,
            bitwarden_client_id_aws_secret_key:
              BITWARDEN_CLIENT_ID_AWS_SECRET_KEY,
            bitwarden_client_secret_aws_secret_key:
              BITWARDEN_CLIENT_SECRET_AWS_SECRET_KEY,
            bitwarden_master_password_aws_secret_key:
              BITWARDEN_MASTER_PASSWORD_AWS_SECRET_KEY,
          };
        } else if (
          parameter.parameterType ===
          WorkflowEditorParameterTypes.CreditCardData
        ) {
          return {
            parameter_type: WorkflowParameterTypes.Bitwarden_Credit_Card_Data,
            key: parameter.key,
            description: parameter.description || null,
            bitwarden_item_id: parameter.itemId,
            bitwarden_collection_id: parameter.collectionId,
            bitwarden_client_id_aws_secret_key:
              BITWARDEN_CLIENT_ID_AWS_SECRET_KEY,
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
          } else if (parameterIsSkyvernCredential(parameter)) {
            return {
              parameter_type: WorkflowParameterTypes.Workflow,
              workflow_parameter_type: WorkflowParameterValueType.CredentialId,
              default_value: parameter.credentialId,
              key: parameter.key,
              description: parameter.description || null,
            };
          } else if (parameterIsOnePasswordCredential(parameter)) {
            return {
              parameter_type: WorkflowParameterTypes.OnePassword,
              key: parameter.key,
              description: parameter.description || null,
              vault_id: parameter.vaultId,
              item_id: parameter.itemId,
            };
          }
        }
        return undefined;
      },
    )
    .filter(
      (
        param:
          | WorkflowParameterYAML
          | BitwardenLoginCredentialParameterYAML
          | ContextParameterYAML
          | BitwardenSensitiveInformationParameterYAML
          | BitwardenCreditCardDataParameterYAML
          | OnePasswordCredentialParameterYAML
          | CredentialParameterYAML
          | undefined,
      ): param is
        | WorkflowParameterYAML
        | BitwardenLoginCredentialParameterYAML
        | ContextParameterYAML
        | BitwardenSensitiveInformationParameterYAML
        | BitwardenCreditCardDataParameterYAML
        | OnePasswordCredentialParameterYAML
        | CredentialParameterYAML => param !== undefined,
    );
}

type Props = {
  nodes: Array<AppNode>;
  edges: Array<Edge>;
  setNodes: (nodes: Array<AppNode>) => void;
  setEdges: (edges: Array<Edge>) => void;
  onNodesChange: (changes: Array<NodeChange<AppNode>>) => void;
  onEdgesChange: (changes: Array<EdgeChange>) => void;
  initialTitle: string;
  // initialParameters: ParametersState;
  workflow: WorkflowApiResponse;
  onDebuggableBlockCountChange: (count: number) => void;
  onMouseDownCapture?: () => void;
  zIndex?: number;
};

function FlowRenderer({
  nodes,
  edges,
  setNodes,
  setEdges,
  onNodesChange,
  onEdgesChange,
  initialTitle,
  // initialParameters,
  workflow,
  onDebuggableBlockCountChange,
  onMouseDownCapture,
  zIndex,
}: Props) {
  const reactFlowInstance = useReactFlow();
  const debugStore = useDebugStore();
  const { title, initializeTitle } = useWorkflowTitleStore();
  // const [parameters] = useState<ParametersState>(initialParameters);
  const parameters = useWorkflowParametersStore((state) => state.parameters);
  const nodesInitialized = useNodesInitialized();
  const [shouldConstrainPan, setShouldConstrainPan] = useState(false);
  const onNodesChangeTimeoutRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    if (nodesInitialized) {
      setShouldConstrainPan(true);
    }
  }, [nodesInitialized]);

  useEffect(() => {
    initializeTitle(initialTitle);
  }, [initialTitle, initializeTitle]);

  const workflowChangesStore = useWorkflowHasChangesStore();
  const setGetSaveDataRef = useRef(workflowChangesStore.setGetSaveData);
  setGetSaveDataRef.current = workflowChangesStore.setGetSaveData;
  const saveWorkflow = useWorkflowSave();
  useShouldNotifyWhenClosingTab(workflowChangesStore.hasChanges);
  const blocker = useBlocker(({ currentLocation, nextLocation }) => {
    return (
      workflowChangesStore.hasChanges &&
      nextLocation.pathname !== currentLocation.pathname
    );
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

  useEffect(() => {
    const topLevelBlocks = getWorkflowBlocks(nodes, edges);
    const debuggable = topLevelBlocks.filter((block) =>
      debuggableWorkflowBlockTypes.has(block.block_type),
    );

    for (const node of nodes) {
      const childBlocks = getOrderedChildrenBlocks(nodes, edges, node.id);

      for (const child of childBlocks) {
        if (debuggableWorkflowBlockTypes.has(child.block_type)) {
          debuggable.push(child);
        }
      }
    }

    onDebuggableBlockCountChange(debuggable.length);
  }, [nodes, edges, onDebuggableBlockCountChange]);

  const constructSaveData = useCallback((): WorkflowSaveData => {
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

    return {
      parameters: [
        ...echoParameters,
        ...parametersInYAMLConvertibleJSON,
        ...emailAwsSecretParameters,
      ],
      blocks,
      title,
      settings,
      workflow,
    };
  }, [nodes, edges, parameters, title, workflow]);

  useEffect(() => {
    setGetSaveDataRef.current(constructSaveData);
  }, [constructSaveData]);

  async function handleSave() {
    return await saveWorkflow.mutateAsync();
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
    workflowChangesStore.setHasChanges(true);

    doLayout(newNodesWithUpdatedParameters, newEdges);
  }

  function toggleScript({
    id,
    label,
    show,
  }: {
    id?: string;
    label?: string;
    show: boolean;
  }) {
    if (id) {
      const node = nodes.find((node) => node.id === id);
      if (!node || !isWorkflowBlockNode(node)) {
        return;
      }

      node.data.showCode = show;
    } else if (label) {
      const node = nodes.find(
        (node) => "label" in node.data && node.data.label === label,
      );

      if (!node || !isWorkflowBlockNode(node)) {
        return;
      }

      node.data.showCode = show;
    }

    doLayout(nodes, edges);
  }

  const editorElementRef = useRef<HTMLDivElement>(null);

  useAutoPan(editorElementRef, nodes);

  const zoomLock = 1 as const;
  const yLockMax = 140 as const;

  /**
   * TODO(jdo): hack
   */
  const getXLock = () => {
    const hasForLoopNode = nodes.some((node) => node.type === "loop");
    return hasForLoopNode ? 24 : 104;
  };

  useOnChange(debugStore.isDebugMode, (newValue) => {
    const xLock = getXLock();
    if (newValue) {
      const currentY = reactFlowInstance.getViewport().y;
      reactFlowInstance.setViewport({ x: xLock, y: currentY, zoom: zoomLock });
    }
  });

  const constrainPan = (viewport: Viewport) => {
    const y = viewport.y;
    const yLockMin = nodes.reduce(
      (acc, node) => {
        const nodeBottom = node.position.y + (node.height ?? 0);
        if (nodeBottom > acc.value) {
          return { value: nodeBottom };
        }
        return acc;
      },
      { value: -Infinity },
    );
    const yLockMinValue = yLockMin.value;
    const xLock = getXLock();
    const newY = Math.max(-yLockMinValue + yLockMax, Math.min(yLockMax, y));

    // avoid infinite recursion with onMove
    if (
      viewport.x !== xLock ||
      viewport.y !== newY ||
      viewport.zoom !== zoomLock
    ) {
      reactFlowInstance.setViewport({
        x: xLock,
        y: newY,
        zoom: zoomLock,
      });
    }
  };

  return (
    <div
      className="h-full w-full"
      style={{ zIndex }}
      onMouseDownCapture={() => onMouseDownCapture?.()}
    >
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
              disabled={workflowChangesStore.saveIsPending}
            >
              {workflowChangesStore.saveIsPending && (
                <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
              )}
              Save changes
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <BlockActionContext.Provider
        value={{
          /**
           * NOTE: defer deletion to next tick to allow React Flow's internal
           * event handlers to complete; removes a console warning from the
           * React Flow library
           */
          deleteNodeCallback: (id: string) =>
            setTimeout(() => deleteNode(id), 0),
          toggleScriptForNodeCallback: toggleScript,
        }}
      >
        <ReactFlow
          ref={editorElementRef}
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
              workflowChangesStore.setHasChanges(true);
            }
            // throttle onNodesChange to prevent cascading React updates
            if (onNodesChangeTimeoutRef.current === null) {
              onNodesChange(changes);
              onNodesChangeTimeoutRef.current = setTimeout(() => {
                onNodesChangeTimeoutRef.current = null;
              }, 33); // ~30fps throttle
            }
          }}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          colorMode="dark"
          fitView={true}
          fitViewOptions={{
            maxZoom: 1,
          }}
          deleteKeyCode={null}
          onMove={(_, viewport) => {
            if (debugStore.isDebugMode && shouldConstrainPan) {
              constrainPan(viewport);
            }
          }}
          maxZoom={debugStore.isDebugMode ? 1 : 2}
          minZoom={debugStore.isDebugMode ? 1 : 0.5}
          panOnDrag={true}
          panOnScroll={true}
          panOnScrollMode={PanOnScrollMode.Vertical}
          zoomOnDoubleClick={!debugStore.isDebugMode}
          zoomOnPinch={!debugStore.isDebugMode}
          zoomOnScroll={!debugStore.isDebugMode}
        >
          <Background variant={BackgroundVariant.Dots} bgColor="#020617" />
          <Controls position="bottom-left" />
        </ReactFlow>
      </BlockActionContext.Provider>
    </div>
  );
}

export { FlowRenderer, type Props as FlowRendererProps };
