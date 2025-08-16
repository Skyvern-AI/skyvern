import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { useState } from "react";
import { helpTooltips } from "../../helpContent";
import { type SendEmailNode } from "./types";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";

function SendEmailNode({ id, data }: NodeProps<SendEmailNode>) {
  const { updateNodeData } = useReactFlow();
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
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
            "pointer-events-none": thisBlockIsPlaying,
            "bg-slate-950 outline outline-2 outline-slate-300":
              thisBlockIsTargetted,
          },
        )}
      >
        <NodeHeader
          blockLabel={label}
          editable={editable}
          nodeId={id}
          totpIdentifier={null}
          totpUrl={null}
          type="send_email" // sic: the naming is not consistent
        />
        <div className="space-y-2">
          <div className="flex justify-between">
            <Label className="text-xs text-slate-300">Recipients</Label>
            {isFirstWorkflowBlock ? (
              <div className="flex justify-end text-xs text-slate-400">
                Tip: Use the {"+"} button to add parameters!
              </div>
            ) : null}
          </div>
          <WorkflowBlockInput
            nodeId={id}
            onChange={(value) => {
              handleChange("recipients", value);
            }}
            value={inputs.recipients}
            placeholder="example@gmail.com, example2@gmail.com..."
            className="nopan text-xs"
          />
        </div>
        <Separator />
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Subject</Label>
          <WorkflowBlockInput
            nodeId={id}
            onChange={(value) => {
              handleChange("subject", value);
            }}
            value={inputs.subject}
            placeholder="What is the gist?"
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Body</Label>
          <WorkflowBlockInputTextarea
            nodeId={id}
            onChange={(value) => {
              handleChange("body", value);
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
            <HelpTooltip
              content={helpTooltips["sendEmail"]["fileAttachments"]}
            />
          </div>
          <WorkflowBlockInput
            nodeId={id}
            value={inputs.fileAttachments}
            onChange={(value) => {
              handleChange("fileAttachments", value);
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
