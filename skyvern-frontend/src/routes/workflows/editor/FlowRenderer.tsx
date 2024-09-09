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
import { WorkflowHeader } from "./WorkflowHeader";
import { AppNode, nodeTypes } from "./nodes";
import "./reactFlowOverrideStyles.css";
import { createNode, getWorkflowBlocks, layout } from "./workflowEditorUtils";
import { useEffect, useState } from "react";
import { WorkflowParametersPanel } from "./panels/WorkflowParametersPanel";
import { edgeTypes } from "./edges";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { WorkflowNodeLibraryPanel } from "./panels/WorkflowNodeLibraryPanel";
import {
  BitwardenLoginCredentialParameterYAML,
  BlockYAML,
  WorkflowParameterYAML,
} from "../types/workflowYamlTypes";
import { WorkflowParametersStateContext } from "./WorkflowParametersStateContext";
import { WorkflowParameterValueType } from "../types/workflowTypes";

function convertToParametersYAML(
  parameters: ParametersState,
): Array<WorkflowParameterYAML | BitwardenLoginCredentialParameterYAML> {
  return parameters.map((parameter) => {
    if (parameter.parameterType === "workflow") {
      return {
        parameter_type: "workflow",
        key: parameter.key,
        description: parameter.description || null,
        workflow_parameter_type: parameter.dataType,
        default_value: null,
      };
    } else {
      return {
        parameter_type: "bitwarden_login_credential",
        key: parameter.key,
        description: parameter.description || null,
        bitwarden_collection_id: parameter.collectionId,
        url_parameter_key: parameter.urlParameterKey,
        bitwarden_client_id_aws_secret_key: "SKYVERN_BITWARDEN_CLIENT_ID",
        bitwarden_client_secret_aws_secret_key:
          "SKYVERN_BITWARDEN_CLIENT_SECRET",
        bitwarden_master_password_aws_secret_key:
          "SKYVERN_BITWARDEN_MASTER_PASSWORD",
      };
    }
  });
}

export type ParametersState = Array<
  | {
      key: string;
      parameterType: "workflow";
      dataType: WorkflowParameterValueType;
      description?: string;
    }
  | {
      key: string;
      parameterType: "credential";
      collectionId: string;
      urlParameterKey: string;
      description?: string;
    }
>;

type Props = {
  initialTitle: string;
  initialNodes: Array<AppNode>;
  initialEdges: Array<Edge>;
  initialParameters: ParametersState;
  handleSave: (
    parameters: Array<
      WorkflowParameterYAML | BitwardenLoginCredentialParameterYAML
    >,
    blocks: Array<BlockYAML>,
    title: string,
  ) => void;
};

export type AddNodeProps = {
  nodeType: Exclude<keyof typeof nodeTypes, "nodeAdder">;
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
  handleSave,
}: Props) {
  const { workflowPanelState, setWorkflowPanelState, closeWorkflowPanel } =
    useWorkflowPanelStore();
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const [parameters, setParameters] = useState(initialParameters);
  const [title, setTitle] = useState(initialTitle);
  const nodesInitialized = useNodesInitialized();

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

  function addNode({
    nodeType,
    previous,
    next,
    parent,
    connectingEdgeType,
  }: AddNodeProps) {
    const newNodes: Array<AppNode> = [];
    const newEdges: Array<Edge> = [];
    const index = parent
      ? nodes.filter((node) => node.parentId === parent).length
      : nodes.length;
    const id = parent ? `${parent}-${index}` : String(index);
    const node = createNode({ id, parentId: parent }, nodeType, String(index));
    newNodes.push(node);
    if (previous) {
      const newEdge = {
        id: `edge-${previous}-${id}`,
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
        id: `edge-${id}-${next}`,
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
      newNodes.push({
        id: `${id}-nodeAdder`,
        type: "nodeAdder",
        parentId: id,
        position: { x: 0, y: 0 },
        data: {},
        draggable: false,
        connectable: false,
      });
    }

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
    doLayout(newNodesAfter, [...editedEdges, ...newEdges]);
  }

  return (
    <WorkflowParametersStateContext.Provider
      value={[parameters, setParameters]}
    >
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
      >
        <Background variant={BackgroundVariant.Dots} bgColor="#020617" />
        <Controls position="bottom-left" />
        <Panel position="top-center" className="h-20">
          <WorkflowHeader
            title={title}
            onTitleChange={setTitle}
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
            onSave={() => {
              const blocksInYAMLConvertibleJSON = getWorkflowBlocks(nodes);
              const parametersInYAMLConvertibleJSON =
                convertToParametersYAML(parameters);
              handleSave(
                parametersInYAMLConvertibleJSON,
                blocksInYAMLConvertibleJSON,
                title,
              );
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
        {nodes.length === 0 && (
          <Panel position="top-right">
            <WorkflowNodeLibraryPanel
              onNodeClick={(props) => {
                addNode(props);
              }}
              first
            />
          </Panel>
        )}
      </ReactFlow>
    </WorkflowParametersStateContext.Provider>
  );
}

export { FlowRenderer };
