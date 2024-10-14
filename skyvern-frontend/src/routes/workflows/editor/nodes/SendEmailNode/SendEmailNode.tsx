import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { EnvelopeClosedIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { useState } from "react";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { helpTooltipContent, type SendEmailNode } from "./types";
import { HelpTooltip } from "@/components/HelpTooltip";

function SendEmailNode({ id, data }: NodeProps<SendEmailNode>) {
  const { updateNodeData } = useReactFlow();
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const deleteNodeCallback = useDeleteNodeCallback();
  const [inputs, setInputs] = useState({
    recipients: data.recipients,
    subject: data.subject,
    body: data.body,
    fileAttachments: data.fileAttachments,
  });

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
      <div className="w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4">
        <div className="flex h-[2.75rem] justify-between">
          <div className="flex gap-2">
            <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
              <EnvelopeClosedIcon className="h-6 w-6" />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={data.editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">Send Email Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </div>
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Recipients</Label>
          <Input
            onChange={(event) => {
              if (!data.editable) {
                return;
              }
              handleChange("recipients", event.target.value);
            }}
            value={inputs.recipients}
            placeholder="example@gmail.com, example2@gmail.com..."
            className="nopan text-xs"
          />
        </div>
        <Separator />
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Subject</Label>
          <Input
            onChange={(event) => {
              if (!data.editable) {
                return;
              }
              handleChange("subject", event.target.value);
            }}
            value={inputs.subject}
            placeholder="What is the gist?"
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Body</Label>
          <Input
            onChange={(event) => {
              if (!data.editable) {
                return;
              }
              handleChange("body", event.target.value);
            }}
            value={inputs.body}
            placeholder="What would you like to say?"
            className="nopan text-xs"
          />
        </div>
        <Separator />
        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">File Attachments</Label>
            <HelpTooltip content={helpTooltipContent["fileAttachments"]} />
          </div>
          <Input
            value={inputs.fileAttachments}
            onChange={(event) => {
              if (!data.editable) {
                return;
              }
              handleChange("fileAttachments", event.target.value);
            }}
            disabled
            className="nopan text-xs"
          />
        </div>
      </div>
    </div>
  );
}

export { SendEmailNode };
