import { useReactFlow } from "@xyflow/react";

import { Label } from "@/components/ui/label";
import { WorkflowBlockInputSet } from "@/components/WorkflowBlockInputSet";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { useWorkflowScopeReadOnly } from "@/routes/workflows/editor/WorkflowScopeContext";
import { deepEqualStringArrays } from "@/util/equality";

import { type AppNode, isWorkflowBlockNode } from "..";
import type { CodeBlockNode, CodeBlockNodeData } from "./types";
import { useUpdate } from "../../useUpdate";

function CodeBlockEditor({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "codeBlock") {
    return null;
  }
  return <CodeBlockEditorBody blockId={blockId} node={node as CodeBlockNode} />;
}

function CodeBlockEditorBody({
  blockId,
  node,
}: {
  blockId: string;
  node: CodeBlockNode;
}) {
  const data = node.data;
  const { editable } = data;
  const update = useUpdate<CodeBlockNodeData>({ id: blockId, editable });
  const scopeReadOnly = useWorkflowScopeReadOnly();

  return (
    <div data-testid="code-block-block-form" className="space-y-4">
      <div className="space-y-2">
        <Label className="text-xs text-slate-300">Inputs</Label>
        <WorkflowBlockInputSet
          nodeId={blockId}
          onChange={(parameterKeys) => {
            const newParameterKeys = Array.from(parameterKeys);
            if (!deepEqualStringArrays(data.parameterKeys, newParameterKeys)) {
              update({ parameterKeys: newParameterKeys });
            }
          }}
          values={new Set(data.parameterKeys ?? [])}
        />
      </div>
      <div className="space-y-2">
        <Label className="text-xs text-slate-300">Code Input</Label>
        <CodeEditor
          language="python"
          value={data.code}
          readOnly={scopeReadOnly}
          onChange={(value) => {
            update({ code: value });
          }}
          className="nopan"
          fontSize={8}
        />
      </div>
    </div>
  );
}

export { CodeBlockEditor };
