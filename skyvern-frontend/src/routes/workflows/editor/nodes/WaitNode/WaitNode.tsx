import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { WorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { useState } from "react";
import { helpTooltips } from "../../helpContent";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import type { WaitNode } from "./types";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { Input } from "@/components/ui/input";

function WaitNode({ id, data }: NodeProps<WaitNode>) {
  const { updateNodeData } = useReactFlow();
  const { editable } = data;
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const [inputs, setInputs] = useState({
    waitInSeconds: data.waitInSeconds,
  });
  const deleteNodeCallback = useDeleteNodeCallback();

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
      <div className="w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4">
        <header className="flex h-[2.75rem] justify-between">
          <div className="flex gap-2">
            <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
              <WorkflowBlockIcon
                workflowBlockType={WorkflowBlockTypes.Wait}
                className="size-6"
              />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">Wait Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </header>
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
