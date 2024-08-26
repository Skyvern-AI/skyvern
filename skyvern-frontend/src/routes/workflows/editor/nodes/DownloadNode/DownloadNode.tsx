import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { DotsHorizontalIcon, DownloadIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position } from "@xyflow/react";
import type { DownloadNode } from "./types";

function DownloadNode({ data }: NodeProps<DownloadNode>) {
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
              <span className="max-w-64 truncate text-base">{data.label}</span>
              <span className="text-xs text-slate-400">Download Block</span>
            </div>
          </div>
          <div>
            <DotsHorizontalIcon className="h-6 w-6" />
          </div>
        </div>
        <div className="space-y-4">
          <div className="space-y-1">
            <Label className="text-sm text-slate-400">File URL</Label>
            <Input
              value={data.url}
              onChange={() => {
                if (!data.editable) {
                  return;
                }
                // TODO
              }}
              className="nopan"
            />
          </div>
        </div>
      </div>
    </div>
  );
}

export { DownloadNode };
