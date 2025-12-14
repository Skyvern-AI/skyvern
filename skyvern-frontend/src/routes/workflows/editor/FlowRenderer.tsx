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
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
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
import { nanoid } from "nanoid";
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
  AzureVaultCredentialParameterYAML,
  ParameterYAML,
  WorkflowParameterYAML,
} from "../types/workflowYamlTypes";
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
import {
  ParametersState,
  parameterIsSkyvernCredential,
  parameterIsOnePasswordCredential,
  parameterIsBitwardenCredential,
  parameterIsAzureVaultCredential,
} from "./types";
import "./reactFlowOverrideStyles.css";
import {
  convertEchoParameters,
  convertToNode,
  createNode,
  descendants,
  generateNodeLabel,
  getAdditionalParametersForEmailBlock,
  getOrderedChildrenBlocks,
  getOutputParameterKey,
  getWorkflowBlocks,
  getWorkflowSettings,
  layout,
  upgradeWorkflowDefinitionToVersionTwo,
} from "./workflowEditorUtils";
import { getWorkflowErrors } from "./workflowEditorUtils";
import { toast } from "@/components/ui/use-toast";
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
  | AzureVaultCredentialParameterYAML
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
        | AzureVaultCredentialParameterYAML
        | CredentialParameterYAML
        | undefined => {
        if (parameter.parameterType === WorkflowEditorParameterTypes.Workflow) {
          // Convert boolean default values to strings for backend
          let defaultValue = parameter.defaultValue;
          if (
            parameter.dataType === "boolean" &&
            typeof parameter.defaultValue === "boolean"
          ) {
            defaultValue = String(parameter.defaultValue);
          }
          if (
            (parameter.dataType === "integer" ||
              parameter.dataType === "float") &&
            (typeof parameter.defaultValue === "number" ||
              typeof parameter.defaultValue === "string")
          ) {
            defaultValue =
              parameter.defaultValue === null
                ? parameter.defaultValue
                : String(parameter.defaultValue);
          }

          return {
            parameter_type: WorkflowParameterTypes.Workflow,
            key: parameter.key,
            description: parameter.description || null,
            workflow_parameter_type: parameter.dataType,
            ...(parameter.defaultValue === null
              ? {}
              : { default_value: defaultValue }),
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
          } else if (parameterIsAzureVaultCredential(parameter)) {
            return {
              parameter_type: WorkflowParameterTypes.Azure_Vault_Credential,
              key: parameter.key,
              description: parameter.description || null,
              vault_name: parameter.vaultName,
              username_key: parameter.usernameKey,
              password_key: parameter.passwordKey,
              totp_secret_key: parameter.totpSecretKey,
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
          | AzureVaultCredentialParameterYAML
          | CredentialParameterYAML
          | undefined,
      ): param is
        | WorkflowParameterYAML
        | BitwardenLoginCredentialParameterYAML
        | ContextParameterYAML
        | BitwardenSensitiveInformationParameterYAML
        | BitwardenCreditCardDataParameterYAML
        | OnePasswordCredentialParameterYAML
        | AzureVaultCredentialParameterYAML
        | CredentialParameterYAML => param !== undefined,
    );
}

type Props = {
  hideBackground?: boolean;
  hideControls?: boolean;
  nodes: Array<AppNode>;
  edges: Array<Edge>;
  setNodes: (nodes: Array<AppNode>) => void;
  setEdges: (edges: Array<Edge>) => void;
  onNodesChange: (changes: Array<NodeChange<AppNode>>) => void;
  onEdgesChange: (changes: Array<EdgeChange>) => void;
  initialTitle: string;
  // initialParameters: ParametersState;
  workflow: WorkflowApiResponse;
  onDebuggableBlockCountChange?: (count: number) => void;
  onMouseDownCapture?: () => void;
  zIndex?: number;
  onContainerResize?: number;
};

function FlowRenderer({
  hideBackground = false,
  hideControls = false,
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
  onContainerResize,
}: Props) {
  const reactFlowInstance = useReactFlow();
  const debugStore = useDebugStore();
  const { title, initializeTitle } = useWorkflowTitleStore();
  // const [parameters] = useState<ParametersState>(initialParameters);
  const parameters = useWorkflowParametersStore((state) => state.parameters);
  const nodesInitialized = useNodesInitialized();
  const [shouldConstrainPan, setShouldConstrainPan] = useState(false);
  const flowIsConstrained = debugStore.isDebugMode;

  // Track if this is the initial load to prevent false "unsaved changes" detection
  const isInitialLoadRef = useRef(true);

  useEffect(() => {
    if (nodesInitialized) {
      setShouldConstrainPan(true);
      // Mark initial load as complete after nodes are initialized
      isInitialLoadRef.current = false;
    }
  }, [nodesInitialized]);

  useEffect(() => {
    initializeTitle(initialTitle);
  }, [initialTitle, initializeTitle]);

  const workflowChangesStore = useWorkflowHasChangesStore();
  const setGetSaveDataRef = useRef(workflowChangesStore.setGetSaveData);
  setGetSaveDataRef.current = workflowChangesStore.setGetSaveData;
  const saveWorkflow = useWorkflowSave({ status: "published" });
  const recordedBlocks = useRecordedBlocksStore((state) => state.blocks);
  const recordedParameters = useRecordedBlocksStore(
    (state) => state.parameters,
  );
  const recordedInsertionPoint = useRecordedBlocksStore(
    (state) => state.insertionPoint,
  );
  const clearRecordedBlocks = useRecordedBlocksStore(
    (state) => state.clearRecordedBlocks,
  );
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

    onDebuggableBlockCountChange?.(debuggable.length);
  }, [nodes, edges, onDebuggableBlockCountChange]);

  const constructSaveData = useCallback((): WorkflowSaveData => {
    const blocks = getWorkflowBlocks(nodes, edges);
    const { blocks: upgradedBlocks, version: workflowDefinitionVersion } =
      upgradeWorkflowDefinitionToVersionTwo(
        blocks,
        workflow.workflow_definition.version,
      );
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
      upgradedBlocks,
      overallParameters,
    );

    return {
      parameters: [
        ...echoParameters,
        ...parametersInYAMLConvertibleJSON,
        ...emailAwsSecretParameters,
      ],
      blocks: upgradedBlocks,
      workflowDefinitionVersion,
      title,
      settings,
      workflow,
    };
  }, [nodes, edges, parameters, title, workflow]);

  useEffect(() => {
    setGetSaveDataRef.current(constructSaveData);
  }, [constructSaveData]);

  async function handleSave(): Promise<boolean> {
    // Validate before saving; block if any workflow errors exist
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
      return false;
    }
    await saveWorkflow.mutateAsync();
    return true;
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

  function transmuteNode(id: string, nodeType: string) {
    const nodeToTransmute = nodes.find((node) => node.id === id);

    if (!nodeToTransmute || !isWorkflowBlockNode(nodeToTransmute)) {
      return;
    }

    const newNode = createNode(
      { id: nodeToTransmute.id, parentId: nodeToTransmute.parentId },
      nodeType as NonNullable<WorkflowBlockNode["type"]>,
      nodeToTransmute.data.label,
    );

    const newNodes = nodes.map((node) => {
      if (node.id === id) {
        return newNode;
      }
      return node;
    });

    workflowChangesStore.setHasChanges(true);
    doLayout(newNodes, edges);
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
      if (!node) {
        return;
      }

      node.data.showCode = show;
    } else if (label) {
      const node = nodes.find(
        (node) => "label" in node.data && node.data.label === label,
      );

      if (!node) {
        return;
      }

      node.data.showCode = show;
    }

    doLayout(nodes, edges);
  }

  // effect to add new blocks that were generated from a browser recording,
  // along with any new parameters
  useEffect(() => {
    if (!recordedBlocks || !recordedInsertionPoint) {
      return;
    }

    const { previous, next, parent, connectingEdgeType } =
      recordedInsertionPoint;

    const newNodes: Array<AppNode> = [];
    const newEdges: Array<Edge> = [];

    let existingLabels = nodes
      .filter(isWorkflowBlockNode)
      .map((node) => node.data.label);

    let prevNodeId = previous;

    // convert each WorkflowBlock to an AppNode
    recordedBlocks.forEach((block, index) => {
      const id = nanoid();
      const label = generateNodeLabel(existingLabels);
      existingLabels = [...existingLabels, label];
      const blockWithLabel = { ...block, label: block.label || label };

      const node = convertToNode(
        { id, parentId: parent },
        blockWithLabel,
        true,
      );
      newNodes.push(node);

      // create edge from previous node to this one
      if (prevNodeId) {
        newEdges.push({
          id: nanoid(),
          type: "edgeWithAddButton",
          source: prevNodeId,
          target: id,
          style: { strokeWidth: 2 },
        });
      }

      // if this is the last block, connect to next
      if (index === recordedBlocks.length - 1 && next) {
        newEdges.push({
          id: nanoid(),
          type: connectingEdgeType,
          source: id,
          target: next,
          style: { strokeWidth: 2 },
        });
      }

      prevNodeId = id;
    });

    const editedEdges = previous
      ? edges.filter((edge) => edge.source !== previous)
      : edges;

    const previousNode = nodes.find((node) => node.id === previous);
    const previousNodeIndex = previousNode
      ? nodes.indexOf(previousNode)
      : nodes.length - 1;

    const newNodesAfter = [
      ...nodes.slice(0, previousNodeIndex + 1),
      ...newNodes,
      ...nodes.slice(previousNodeIndex + 1),
    ];

    workflowChangesStore.setHasChanges(true);
    doLayout(newNodesAfter, [...editedEdges, ...newEdges]);

    const newParameters = Array<ParametersState[number]>();

    for (const newParameter of recordedParameters ?? []) {
      const exists = parameters.some((param) => param.key === newParameter.key);

      if (!exists) {
        newParameters.push({
          key: newParameter.key,
          parameterType: "workflow",
          dataType: newParameter.workflow_parameter_type,
          description: newParameter.description ?? null,
          defaultValue: newParameter.default_value ?? "",
        });
      }
    }

    if (newParameters.length > 0) {
      const workflowParametersStore = useWorkflowParametersStore.getState();
      workflowParametersStore.setParameters([
        ...workflowParametersStore.parameters,
        ...newParameters,
      ]);
    }

    clearRecordedBlocks();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recordedBlocks, recordedInsertionPoint]);

  const editorElementRef = useRef<HTMLDivElement>(null);

  useAutoPan(editorElementRef, nodes);

  useEffect(() => {
    doLayout(nodes, edges);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onContainerResize]);

  const zoomLock = 1 as const;
  const yLockMax = 140 as const;

  /**
   * TODO(jdo): hack
   *
   * Locks the x position of the flow to an ideal x based on the ideal width
   * of the flow. The ideal width is based on differently-width'd blocks.
   */
  const getXLock = () => {
    const rect = editorElementRef.current?.getBoundingClientRect();

    if (!rect) {
      return 24;
    }

    const width = rect.width;
    const hasLoopBlock = nodes.some((node) => node.type === "loop");
    const hasHttpBlock = nodes.some((node) => node.type === "http_request");
    const idealWidth = hasHttpBlock ? 580 : hasLoopBlock ? 498 : 475;
    const split = (width - idealWidth) / 2;

    return Math.max(24, split);
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
                handleSave().then((ok) => {
                  if (ok) {
                    blocker.proceed?.();
                  }
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
          transmuteNodeCallback: (id: string, nodeName: string) =>
            setTimeout(() => transmuteNode(id, nodeName), 0),
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

            // Only track changes after initial load is complete
            if (
              !isInitialLoadRef.current &&
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

            onNodesChange(changes);
          }}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          // colorMode="dark"
          fitView={true}
          fitViewOptions={{
            maxZoom: 1,
          }}
          deleteKeyCode={null}
          onMove={(_, viewport) => {
            if (flowIsConstrained && shouldConstrainPan) {
              constrainPan(viewport);
            }
          }}
          maxZoom={flowIsConstrained ? 1 : 2}
          minZoom={flowIsConstrained ? 1 : 0.5}
          panOnDrag={true}
          panOnScroll={true}
          panOnScrollMode={PanOnScrollMode.Vertical}
          zoomOnDoubleClick={!flowIsConstrained}
          zoomOnPinch={!flowIsConstrained}
          zoomOnScroll={!flowIsConstrained}
        >
          {!hideBackground && (
            <Background variant={BackgroundVariant.Dots} bgColor="#020617" />
          )}
          {!hideControls && <Controls position="bottom-left" />}
        </ReactFlow>
      </BlockActionContext.Provider>
    </div>
  );
}

export { FlowRenderer, type Props as FlowRendererProps };
