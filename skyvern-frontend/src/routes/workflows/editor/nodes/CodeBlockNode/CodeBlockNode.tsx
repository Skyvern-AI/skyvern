import { useParams } from "react-router-dom";
import { Handle, NodeProps, Position } from "@xyflow/react";

import { Label } from "@/components/ui/label";
import { WorkflowBlockInputSet } from "@/components/WorkflowBlockInputSet";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { useRecordingStore } from "@/store/useRecordingStore";
import { deepEqualStringArrays } from "@/util/equality";
import { cn } from "@/util/utils";

import type { CodeBlockNode } from "./types";
import { NodeHeader } from "../components/NodeHeader";

function CodeBlockNode({ id, data }: NodeProps<CodeBlockNode>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const recordingStore = useRecordingStore();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const update = useUpdate<CodeBlockNode["data"]>({ id, editable });

  return (
    <div
      className={cn({
        "pointer-events-none opacity-50": recordingStore.isRecording,
      })}
    >
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
              const newParameterKeys = Array.from(parameterKeys);
              if (
                !deepEqualStringArrays(data.parameterKeys, newParameterKeys)
              ) {
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
            onChange={(value) => {
              update({ code: value });
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
