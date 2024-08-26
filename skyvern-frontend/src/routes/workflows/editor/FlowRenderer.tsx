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
import { layout } from "./workflowEditorUtils";
import { useEffect, useState } from "react";
import { WorkflowParametersPanel } from "./panels/WorkflowParametersPanel";

type Props = {
  title: string;
  initialNodes: Array<AppNode>;
  initialEdges: Array<Edge>;
};

function FlowRenderer({ title, initialEdges, initialNodes }: Props) {
  const [rightSidePanelOpen, setRightSidePanelOpen] = useState(false);
  const [rightSidePanelContent, setRightSidePanelContent] = useState<
    "parameters" | "nodeLibrary" | null
  >(null);
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
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

  return (
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
          parametersPanelOpen={rightSidePanelOpen}
          onParametersClick={() => {
            setRightSidePanelOpen((open) => !open);
            setRightSidePanelContent("parameters");
          }}
        />
      </Panel>
      {rightSidePanelOpen && (
        <Panel
          position="top-right"
          className="w-96 rounded-xl border border-slate-700 bg-slate-950 p-5 shadow-xl"
        >
          {rightSidePanelContent === "parameters" && (
            <WorkflowParametersPanel />
          )}
        </Panel>
      )}
    </ReactFlow>
  );
}

export { FlowRenderer };
