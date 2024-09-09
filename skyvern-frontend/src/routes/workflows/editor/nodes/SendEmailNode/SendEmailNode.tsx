import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import type { SendEmailNode } from "./types";
import { DotsHorizontalIcon, EnvelopeClosedIcon } from "@radix-ui/react-icons";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { EditableNodeTitle } from "../components/EditableNodeTitle";

function SendEmailNode({ id, data }: NodeProps<SendEmailNode>) {
  const { updateNodeData } = useReactFlow();

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
              <EditableNodeTitle
                value={data.label}
                editable={data.editable}
                onChange={(value) => updateNodeData(id, { label: value })}
              />
              <span className="text-xs text-slate-400">Send Email Block</span>
            </div>
          </div>
          <div>
            <DotsHorizontalIcon className="h-6 w-6" />
          </div>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-slate-300">Sender</Label>
          <Input
            onChange={(event) => {
              if (!data.editable) {
                return;
              }
              updateNodeData(id, { sender: event.target.value });
            }}
            value={data.sender}
            placeholder="example@gmail.com"
            className="nopan"
          />
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-slate-300">Recipients</Label>
          <Input
            onChange={(event) => {
              if (!data.editable) {
                return;
              }
              updateNodeData(id, { recipients: event.target.value });
            }}
            value={data.recipients}
            placeholder="example@gmail.com, example2@gmail.com..."
            className="nopan"
          />
        </div>
        <Separator />
        <div className="space-y-1">
          <Label className="text-xs text-slate-300">Subject</Label>
          <Input
            onChange={(event) => {
              if (!data.editable) {
                return;
              }
              updateNodeData(id, { subject: event.target.value });
            }}
            value={data.subject}
            placeholder="What is the gist?"
            className="nopan"
          />
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-slate-300">Body</Label>
          <Input
            onChange={(event) => {
              if (!data.editable) {
                return;
              }
              updateNodeData(id, { body: event.target.value });
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
            value={data.fileAttachments}
            onChange={(event) => {
              if (!data.editable) {
                return;
              }
              updateNodeData(id, { fileAttachments: event.target.value });
            }}
            className="nopan"
          />
        </div>
      </div>
    </div>
  );
}

export { SendEmailNode };
