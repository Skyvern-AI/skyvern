import { useEffect, useState } from "react";
import { Handle, NodeProps, Position } from "@xyflow/react";
import { useParams } from "react-router-dom";

import { Flippable } from "@/components/Flippable";
import { BlockCodeEditor } from "@/routes/workflows/components/BlockCodeEditor";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useBlockScriptStore } from "@/store/BlockScriptStore";
import { cn } from "@/util/utils";

import { BuildModeOnly } from "../BuildModeOnly";
import { NodeHeader } from "../components/NodeHeader";
import { NodeTabs } from "../components/NodeTabs";
import { Taskv2Editor } from "./Taskv2Editor";
import { type Taskv2Node } from "./types";

function Taskv2Node({ id, data }: NodeProps<Taskv2Node>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const [facing, setFacing] = useState<"front" | "back">("front");
  const blockScriptStore = useBlockScriptStore();
  const script = blockScriptStore.scripts[label];

  useEffect(() => {
    setFacing(data.showCode ? "back" : "front");
  }, [data.showCode]);

  return (
    <Flippable facing={facing} preserveFrontsideHeight={true}>
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
            "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-shadow",
            {
              "pointer-events-none": thisBlockIsPlaying,
              "bg-background outline outline-2 outline-ring":
                thisBlockIsTargetted,
            },
          )}
        >
          <NodeHeader
            blockLabel={label}
            editable={editable}
            nodeId={id}
            totpIdentifier={data.totpIdentifier}
            totpUrl={data.totpVerificationUrl}
            type="task_v2"
          />
          <BuildModeOnly>
            <Taskv2Editor blockId={id} />
          </BuildModeOnly>
          <NodeTabs blockLabel={label} />
        </div>
      </div>
      <BlockCodeEditor blockLabel={label} blockType="task_v2" script={script} />
    </Flippable>
  );
}

export { Taskv2Node };
