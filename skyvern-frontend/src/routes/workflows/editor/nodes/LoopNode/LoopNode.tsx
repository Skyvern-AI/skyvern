import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { WorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import type { Node } from "@xyflow/react";
import {
  Handle,
  NodeProps,
  Position,
  useNodes,
  useReactFlow,
} from "@xyflow/react";
import { AppNode } from "..";
import { helpTooltips } from "../../helpContent";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import type { LoopNode } from "./types";
import { useState } from "react";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { Checkbox } from "@/components/ui/checkbox";
import { getLoopNodeWidth } from "../../workflowEditorUtils";

function LoopNode({ id, data }: NodeProps<LoopNode>) {
  const { updateNodeData } = useReactFlow();
  const nodes = useNodes<AppNode>();
  const node = nodes.find((n) => n.id === id);
  if (!node) {
    throw new Error("Node not found"); // not possible
  }
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const [inputs, setInputs] = useState({
    loopVariableReference: data.loopVariableReference,
  });
  const deleteNodeCallback = useDeleteNodeCallback();

  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });

  const children = nodes.filter((node) => node.parentId === id);
  const furthestDownChild: Node | null = children.reduce(
    (acc, child) => {
      if (!acc) {
        return child;
      }
      if (child.position.y > acc.position.y) {
        return child;
      }
      return acc;
    },
    null as Node | null,
  );

  const childrenHeightExtent =
    (furthestDownChild?.measured?.height ?? 0) +
    (furthestDownChild?.position.y ?? 0) +
    24;

  const loopNodeWidth = getLoopNodeWidth(node, nodes);
  function handleChange(key: string, value: unknown) {
    if (!data.editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

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
        className="rounded-xl border-2 border-dashed border-slate-600 p-2"
        style={{
          width: loopNodeWidth,
          height: childrenHeightExtent,
        }}
      >
        <div className="flex w-full justify-center">
          <div className="w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4">
            <div className="flex h-[2.75rem] justify-between">
              <div className="flex gap-2">
                <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
                  <WorkflowBlockIcon
                    workflowBlockType={WorkflowBlockTypes.ForLoop}
                    className="size-6"
                  />
                </div>
                <div className="flex flex-col gap-1">
                  <EditableNodeTitle
                    value={label}
                    editable={data.editable}
                    onChange={setLabel}
                    titleClassName="text-base"
                    inputClassName="text-base"
                  />
                  <span className="text-xs text-slate-400">Loop Block</span>
                </div>
              </div>
              <NodeActionMenu
                onDelete={() => {
                  deleteNodeCallback(id);
                }}
              />
            </div>
            <div className="space-y-2">
              <div className="flex justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Loop Value</Label>
                  <HelpTooltip content={helpTooltips["loop"]["loopValue"]} />
                </div>
                {isFirstWorkflowBlock ? (
                  <div className="flex justify-end text-xs text-slate-400">
                    Tip: Use the {"+"} button to add parameters!
                  </div>
                ) : null}
              </div>
              <WorkflowBlockInput
                nodeId={id}
                value={inputs.loopVariableReference}
                onChange={(value) => {
                  handleChange("loopVariableReference", value);
                }}
              />
            </div>
            <div className="space-y-2">
              <div className="space-y-2">
                <div className="flex gap-4">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      Complete if Empty
                    </Label>
                    <HelpTooltip content="When checked, this block will successfully complete when the loop value is an empty list" />
                  </div>
                  <Checkbox
                    checked={data.completeIfEmpty}
                    disabled={!data.editable}
                    onCheckedChange={(checked) => {
                      handleChange("completeIfEmpty", checked);
                    }}
                  />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export { LoopNode };
