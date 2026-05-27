import { useNodesData } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";

import { AI_IMPROVE_CONFIGS } from "../../constants";
import {
  type HumanInteractionNode,
  type HumanInteractionNodeData,
} from "./types";
import { useUpdate } from "../../useUpdate";

const instructionsTooltip =
  "Instructions shown to the user for review. Explain what needs to be reviewed and what action should be taken.";
const positiveDescriptorTooltip =
  "Label for the positive action button (e.g., 'Approve', 'Continue', 'Yes').";
const negativeDescriptorTooltip =
  "Label for the negative action button (e.g., 'Reject', 'Cancel', 'No').";
const timeoutTooltip =
  "Time in seconds to wait for human interaction before timing out. Default is 2 hours (7200 seconds).";

function HumanInteractionEditor({ blockId }: { blockId: string }) {
  // Subscribe to this node's data slice. The sidebar mount lives outside
  // the per-node renderer, so a useReactFlow().getNode(id) snapshot does
  // not re-render after updateNodeData commits or undo/redo replays.
  const nodeSlice = useNodesData<HumanInteractionNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "human_interaction") {
    return null;
  }
  return <HumanInteractionEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function HumanInteractionEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: HumanInteractionNodeData;
}) {
  const {
    editable,
    instructions,
    timeoutSeconds,
    recipients,
    subject,
    body,
    negativeDescriptor,
    positiveDescriptor,
  } = data;
  const update = useUpdate<HumanInteractionNodeData>({
    id: blockId,
    editable,
  });

  return (
    <div data-testid="human-interaction-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex justify-between">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">
              Instructions For Human
            </Label>
            <HelpTooltip content={instructionsTooltip} />
          </div>
        </div>
        <WorkflowBlockInput
          nodeId={blockId}
          onChange={(next) => update({ instructions: next })}
          value={instructions}
          placeholder="Please review and approve or reject to continue the agent."
          className="nopan text-xs"
        />
      </div>
      <div className="space-between flex items-center gap-2">
        <Label className="text-xs text-slate-300">Timeout (minutes)</Label>
        <HelpTooltip content={timeoutTooltip} />
        <Input
          className="ml-auto w-16 text-right"
          value={timeoutSeconds / 60}
          placeholder="120"
          onChange={(event) => {
            if (!editable) {
              return;
            }
            const next = Number(event.target.value);
            update({ timeoutSeconds: next * 60 });
          }}
        />
      </div>
      <div className="workflow-editor-tip flex items-center justify-center gap-2 rounded-md bg-slate-800 p-2">
        <span className="workflow-editor-tip-icon rounded bg-slate-700 p-1 text-lg">
          💡
        </span>
        <div className="space-y-1 text-xs text-slate-400">
          The agent will pause and send an email notification to the recipients.
          The agent continues or terminates based on the user's response.
        </div>
      </div>
      <div className="space-y-4 rounded-md bg-slate-800 p-4">
        <h2>Email Settings</h2>
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Recipients</Label>
          <WorkflowBlockInput
            nodeId={blockId}
            onChange={(next) => update({ recipients: next })}
            value={recipients}
            placeholder="example@gmail.com, example2@gmail.com..."
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Subject</Label>
          <WorkflowBlockInput
            nodeId={blockId}
            onChange={(next) => update({ subject: next })}
            value={subject}
            placeholder="Human interaction required for agent run"
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Body</Label>
          <WorkflowBlockInputTextarea
            aiImprove={AI_IMPROVE_CONFIGS.humanInteraction.body}
            nodeId={blockId}
            onChange={(next) => update({ body: next })}
            value={body}
            placeholder="Your interaction is required for an agent run!"
            className="nopan text-xs"
          />
        </div>
      </div>
      <Separator />
      <Accordion type="single" collapsible defaultValue="advanced">
        <AccordionItem value="advanced" className="border-b-0">
          <AccordionTrigger className="py-0">
            Advanced Settings
          </AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-1">
            <div className="space-y-4 pt-4">
              <div className="flex gap-4">
                <div className="flex-1 space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      Negative Button Label
                    </Label>
                    <HelpTooltip content={negativeDescriptorTooltip} />
                  </div>
                  <WorkflowBlockInput
                    nodeId={blockId}
                    onChange={(next) => update({ negativeDescriptor: next })}
                    value={negativeDescriptor}
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
                  <WorkflowBlockInput
                    nodeId={blockId}
                    onChange={(next) => update({ positiveDescriptor: next })}
                    value={positiveDescriptor}
                    placeholder="Approve"
                    className="nopan text-xs"
                  />
                </div>
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { HumanInteractionEditor };
