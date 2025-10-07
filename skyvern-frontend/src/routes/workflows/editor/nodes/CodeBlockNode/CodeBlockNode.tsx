import { Label } from "@/components/ui/label";
import { WorkflowBlockInputSet } from "@/components/WorkflowBlockInputSet";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { useState } from "react";
import type { CodeBlockNode } from "./types";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";

function CodeBlockNode({ id, data }: NodeProps<CodeBlockNode>) {
  const { updateNodeData } = useReactFlow();
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const [inputs, setInputs] = useState({
    code: data.code,
    parameterKeys: data.parameterKeys,
  });

  return (
    <div>
      <Handle
        type="source"
        position={Position.Bottom}
        id="a"
        className="opacity-0"
      />
      <Handle
        type="target"
        position={Position.Top}
        id="b"
        className="opacity-0"
      />
      <div
        className={cn(
          "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-all",
          {
            "pointer-events-none": thisBlockIsPlaying,
            "bg-slate-950 outline outline-2 outline-slate-300":
              thisBlockIsTargetted,
          },
          data.comparisonColor,
        )}
      >
        <NodeHeader
          blockLabel={label}
          editable={editable}
          nodeId={id}
          totpIdentifier={null}
          totpUrl={null}
          type="code" // sic: the naming is not consistent
        />
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Input Parameters</Label>
          <WorkflowBlockInputSet
            nodeId={id}
            onChange={(parameterKeys) => {
              setInputs({
                ...inputs,
                parameterKeys: Array.from(parameterKeys),
              });
              updateNodeData(id, { parameterKeys: Array.from(parameterKeys) });
            }}
            values={new Set(inputs.parameterKeys ?? [])}
          />
        </div>
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Code Input</Label>
          <CodeEditor
            language="python"
            value={inputs.code}
            onChange={(value) => {
              if (!data.editable) {
                return;
              }
              setInputs({ ...inputs, code: value });
              updateNodeData(id, { code: value });
            }}
            className="nopan"
            fontSize={8}
          />
        </div>
      </div>
    </div>
  );
}

export { CodeBlockNode };
