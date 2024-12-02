import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { DownloadIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position } from "@xyflow/react";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { helpTooltipContent, type DownloadNode } from "./types";
import { HelpTooltip } from "@/components/HelpTooltip";

function DownloadNode({ id, data }: NodeProps<DownloadNode>) {
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const deleteNodeCallback = useDeleteNodeCallback();

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
        <div className="flex h-[2.75rem] justify-between">
          <div className="flex gap-2">
            <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
              <DownloadIcon className="h-6 w-6" />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={data.editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">Download Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </div>
        <div className="space-y-4">
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-slate-400">File Path</Label>
              <HelpTooltip content={helpTooltipContent["url"]} />
            </div>
            <Input value={data.url} disabled className="nopan text-xs" />
          </div>
        </div>
      </div>
    </div>
  );
}

export { DownloadNode };
