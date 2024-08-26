import { Handle, NodeProps, Position } from "@xyflow/react";
import type { SendEmailNode } from "./types";
import { DotsHorizontalIcon, EnvelopeClosedIcon } from "@radix-ui/react-icons";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";

function SendEmailNode({ data }: NodeProps<SendEmailNode>) {
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
              <EnvelopeClosedIcon className="h-6 w-6" />
            </div>
            <div className="flex flex-col gap-1">
              <span className="max-w-64 truncate text-base">{data.label}</span>
              <span className="text-xs text-slate-400">Send Email Block</span>
            </div>
          </div>
          <div>
            <DotsHorizontalIcon className="h-6 w-6" />
          </div>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-slate-300">Recipient</Label>
          <Input
            onChange={() => {
              if (!data.editable) return;
              // TODO
            }}
            value={data.recipients.join(", ")}
            placeholder="example@gmail.com"
            className="nopan"
          />
        </div>
        <Separator />
        <div className="space-y-1">
          <Label className="text-xs text-slate-300">Subject</Label>
          <Input
            onChange={() => {
              if (!data.editable) return;
              // TODO
            }}
            value={data.subject}
            placeholder="What is the gist?"
            className="nopan"
          />
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-slate-300">Body</Label>
          <Input
            onChange={() => {
              if (!data.editable) return;
              // TODO
            }}
            value={data.body}
            placeholder="What would you like to say?"
            className="nopan"
          />
        </div>
        <Separator />
        <div className="space-y-1">
          <Label className="text-xs text-slate-300">File Attachments</Label>
          <Input
            value={data.fileAttachments?.join(", ") ?? ""}
            onChange={() => {
              if (!data.editable) return;
              // TODO
            }}
            className="nopan"
          />
        </div>
        <Separator />
        <div className="flex items-center gap-10">
          <Label className="text-xs text-slate-300">
            Attach all downloaded files
          </Label>
          <Switch />
        </div>
      </div>
    </div>
  );
}

export { SendEmailNode };
