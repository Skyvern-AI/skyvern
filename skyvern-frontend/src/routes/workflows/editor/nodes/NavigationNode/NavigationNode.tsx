import { useEffect, useState } from "react";
import { Handle, NodeProps, Position } from "@xyflow/react";
import { useParams } from "react-router-dom";

import { Flippable } from "@/components/Flippable";
import { RunEngine } from "@/api/types";
import { BlockCodeEditor } from "@/routes/workflows/components/BlockCodeEditor";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useBlockScriptStore } from "@/store/BlockScriptStore";
import { cn } from "@/util/utils";

import { useCollapseContext } from "../../collapse/CollapseContext";
import { NodeBody } from "../../collapse/NodeBody";
import { BuildModeOnly } from "../BuildModeOnly";
import { NodeHeader } from "../components/NodeHeader";
import { NodeTabs } from "../components/NodeTabs";
import { NavigationEditor } from "./NavigationEditor";
import { type NavigationNode } from "./types";

function NavigationNode({ id, data, type }: NodeProps<NavigationNode>) {
  const { blockLabel: urlBlockLabel } = useParams();
  const [facing, setFacing] = useState<"front" | "back">("front");
  const blockScriptStore = useBlockScriptStore();
  const { editable, label } = data;
  const script = blockScriptStore.scripts[label];
  const { open } = useCollapseContext();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;

  const isV2Mode = data.engine === RunEngine.SkyvernV2;

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
            "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-shadow motion-reduce:transition-none",
            open ? "shadow-md" : "shadow-sm",
            {
              "pointer-events-none": thisBlockIsPlaying,
              "bg-background outline outline-2 outline-ring":
                thisBlockIsTargetted,
            },
            data.comparisonColor,
          )}
        >
          <NodeHeader
            blockLabel={label}
            editable={editable}
            nodeId={id}
            totpIdentifier={data.totpIdentifier}
            totpUrl={data.totpVerificationUrl}
            type={isV2Mode ? "task_v2" : type}
          />
          <NodeBody>
            <BuildModeOnly>
              <NavigationEditor blockId={id} />
            </BuildModeOnly>
            <NodeTabs blockLabel={label} />
          </NodeBody>
        </div>
      </div>
      <BlockCodeEditor
        blockLabel={label}
        blockType={isV2Mode ? "task_v2" : type}
        script={script}
      />
    </Flippable>
  );
}

export { NavigationNode };
