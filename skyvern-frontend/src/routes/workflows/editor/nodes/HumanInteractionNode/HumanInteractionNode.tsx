import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Input } from "@/components/ui/input";
import { Handle, NodeProps, Position } from "@xyflow/react";
import { type HumanInteractionNode } from "./types";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { useRerender } from "@/hooks/useRerender";

const instructionsTooltip =
  "Instructions shown to the user for review. Explain what needs to be reviewed and what action should be taken.";
const positiveDescriptorTooltip =
  "Label for the positive action button (e.g., 'Approve', 'Continue', 'Yes').";
const negativeDescriptorTooltip =
  "Label for the negative action button (e.g., 'Reject', 'Cancel', 'No').";
const timeoutTooltip =
  "Time in seconds to wait for human interaction before timing out. Default is 2 hours (7200 seconds).";

function HumanInteractionNode({
  id,
  data,
  type,
}: NodeProps<HumanInteractionNode>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const update = useUpdate<HumanInteractionNode["data"]>({ id, editable });
  const rerender = useRerender({ prefix: "accordian" });

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
          data.comparisonColor,
        )}
      >
        <NodeHeader
          blockLabel={label}
          editable={editable}
          nodeId={id}
          totpIdentifier={null}
          totpUrl={null}
          type={type}
        />
        <div
          className={cn("space-y-4", {
            "opacity-50": thisBlockIsPlaying,
          })}
        >
          <div className="space-y-2">
            <div className="flex justify-between">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">
                  Instructions For Human
                </Label>
                <HelpTooltip content={instructionsTooltip} />
              </div>
            </div>
            {/* TODO(jdo): 'instructions' allows templating; but it requires adding a column to the workflow_block_runs
            table, and I don't want to do that just yet (see /timeline endpoint) */}
            <Input
              onChange={(event) => {
                update({ instructions: event.target.value });
              }}
              value={data.instructions}
              placeholder="Please review and approve or reject to continue the workflow."
              className="nopan text-xs"
            />
          </div>
          <div className="flex gap-4">
            <div className="flex-1 space-y-2">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">
                  Negative Button Label
                </Label>
                <HelpTooltip content={negativeDescriptorTooltip} />
              </div>
              <Input
                onChange={(event) => {
                  update({ negativeDescriptor: event.target.value });
                }}
                value={data.negativeDescriptor}
                placeholder="Reject"
                className="nopan text-xs"
              />
            </div>
            <div className="flex-1 space-y-2">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">
                  Positive Button Label
                </Label>
                <HelpTooltip content={positiveDescriptorTooltip} />
              </div>
              <Input
                onChange={(event) => {
                  update({ positiveDescriptor: event.target.value });
                }}
                value={data.positiveDescriptor}
                placeholder="Approve"
                className="nopan text-xs"
              />
            </div>
          </div>
          {/* <div className="space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">
                Timeout (seconds)
              </Label>
              <HelpTooltip content={timeoutTooltip} />
            </div>
            <Input
              type="number"
              className="nopan text-xs"
              min="1"
              value={data.timeoutSeconds}
              onChange={(event) => {
                if (!editable) {
                  return;
                }
                const value = Number(event.target.value);
                update({ timeoutSeconds: value });
              }}
              placeholder="7200"
            />
          </div> */}
          <div className="space-between flex items-center gap-2">
            <Label className="text-xs text-slate-300">Timeout (seconds)</Label>
            <HelpTooltip content={timeoutTooltip} />
            <Input
              className="ml-auto w-16 text-right"
              value={data.timeoutSeconds}
              placeholder="7200"
              onChange={(event) => {
                if (!editable) {
                  return;
                }
                const value = Number(event.target.value);
                update({ timeoutSeconds: value });
              }}
            />
          </div>
          <div className="rounded-md bg-slate-800 p-2">
            <div className="space-y-1 text-xs text-slate-400">
              Tip: The workflow will pause and send an email notification to the
              recipients. The workflow continues or terminates based on the
              user's response.
            </div>
          </div>
        </div>
        <Separator />
        <Accordion
          className={cn({
            "pointer-events-none opacity-50": thisBlockIsPlaying,
          })}
          type="single"
          onValueChange={() => rerender.bump()}
          collapsible
        >
          <AccordionItem value="email" className="border-b-0">
            <AccordionTrigger className="py-0">Email Settings</AccordionTrigger>
            <AccordionContent className="pl-6 pr-1 pt-1">
              <div key={rerender.key} className="space-y-4">
                <div className="space-y-2">
                  <Label className="text-xs text-slate-300">Recipients</Label>
                  <WorkflowBlockInput
                    nodeId={id}
                    onChange={(value) => {
                      update({ recipients: value });
                    }}
                    value={data.recipients}
                    placeholder="example@gmail.com, example2@gmail.com..."
                    className="nopan text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <Label className="text-xs text-slate-300">Subject</Label>
                  <WorkflowBlockInput
                    nodeId={id}
                    onChange={(value) => {
                      update({ subject: value });
                    }}
                    value={data.subject}
                    placeholder="Human interaction required for workflow run"
                    className="nopan text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <Label className="text-xs text-slate-300">Body</Label>
                  <WorkflowBlockInputTextarea
                    nodeId={id}
                    onChange={(value) => {
                      update({ body: value });
                    }}
                    value={data.body}
                    placeholder="Your interaction is required for a workflow run!"
                    className="nopan text-xs"
                  />
                </div>
              </div>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </div>
    </div>
  );
}

export { HumanInteractionNode };
