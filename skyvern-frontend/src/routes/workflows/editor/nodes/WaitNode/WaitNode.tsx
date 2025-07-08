import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { useState } from "react";
import { helpTooltips } from "../../helpContent";
import type { WaitNode } from "./types";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { Input } from "@/components/ui/input";
import { useDebugStore } from "@/store/useDebugStore";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";

function WaitNode({ id, data, type }: NodeProps<WaitNode>) {
  const { updateNodeData } = useReactFlow();
  const { debuggable, editable, label } = data;
  const debugStore = useDebugStore();
  const elideFromDebugging = debugStore.isDebugMode && !debuggable;
  const { blockLabel: urlBlockLabel } = useParams();
  const thisBlockIsPlaying =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const [inputs, setInputs] = useState({
    waitInSeconds: data.waitInSeconds,
  });

  function handleChange(key: string, value: unknown) {
    if (!editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });

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
            "pointer-events-none bg-slate-950 outline outline-2 outline-slate-300":
              thisBlockIsPlaying,
          },
        )}
      >
        <NodeHeader
          blockLabel={label}
          disabled={elideFromDebugging}
          editable={editable}
          nodeId={id}
          totpIdentifier={null}
          totpUrl={null}
          type={type}
        />
        <div className="space-y-2">
          <div className="flex justify-between">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">
                Wait Time (in seconds)
              </Label>
              <HelpTooltip content={helpTooltips["wait"]["waitInSeconds"]} />
            </div>
            {isFirstWorkflowBlock ? (
              <div className="flex justify-end text-xs text-slate-400">
                Tip: Use the {"+"} button to add parameters!
              </div>
            ) : null}
          </div>
          <Input
            value={inputs.waitInSeconds}
            onChange={(event) => {
              handleChange("waitInSeconds", event.target.value);
            }}
            className="nopan text-xs"
          />
        </div>
      </div>
    </div>
  );
}

export { WaitNode };
