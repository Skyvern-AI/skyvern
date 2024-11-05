import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { UpdateIcon } from "@radix-ui/react-icons";
import type { Node } from "@xyflow/react";
import {
  Handle,
  NodeProps,
  Position,
  useEdges,
  useNodes,
  useReactFlow,
} from "@xyflow/react";
import { AppNode } from "..";
import { useWorkflowParametersState } from "../../useWorkflowParametersState";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { helpTooltipContent, type LoopNode } from "./types";
import { HelpTooltip } from "@/components/HelpTooltip";

function LoopNode({ id, data }: NodeProps<LoopNode>) {
  const { updateNodeData } = useReactFlow();
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const deleteNodeCallback = useDeleteNodeCallback();

  const [workflowParameters] = useWorkflowParametersState();
  const parameters = workflowParameters.filter(
    (parameter) => parameter.parameterType !== "credential",
  );
  const parameterKeys = parameters.map((parameter) => parameter.key);
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);

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
        className="w-[600px] rounded-xl border-2 border-dashed border-slate-600 p-2"
        style={{
          height: childrenHeightExtent,
        }}
      >
        <div className="flex w-full justify-center">
          <div className="w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4">
            <div className="flex h-[2.75rem] justify-between">
              <div className="flex gap-2">
                <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
                  <UpdateIcon className="h-6 w-6" />
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
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">
                  Loop Value Parameter
                </Label>
                <HelpTooltip content={helpTooltipContent["loopValue"]} />
              </div>
              <Select
                value={data.loopValue}
                onValueChange={(value) => {
                  updateNodeData(id, { loopValue: value });
                }}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Select the parameter to iterate over" />
                </SelectTrigger>
                <SelectContent>
                  {[...parameterKeys, ...outputParameterKeys].map(
                    (parameterKey) => (
                      <SelectItem key={parameterKey} value={parameterKey}>
                        {parameterKey}
                      </SelectItem>
                    ),
                  )}
                </SelectContent>
              </Select>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export { LoopNode };
