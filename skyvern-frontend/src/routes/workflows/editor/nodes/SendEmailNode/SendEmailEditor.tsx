import { useReactFlow } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";

import { AI_IMPROVE_CONFIGS } from "../../constants";
import { helpTooltips } from "../../helpContent";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { type AppNode, isWorkflowBlockNode } from "..";
import { type SendEmailNode } from "./types";
import { useUpdate } from "../../useUpdate";

function SendEmailEditor({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "sendEmail") {
    return null;
  }
  return <SendEmailEditorBody blockId={blockId} node={node as SendEmailNode} />;
}

function SendEmailEditorBody({
  blockId,
  node,
}: {
  blockId: string;
  node: SendEmailNode;
}) {
  const { editable, recipients, subject, body, fileAttachments } = node.data;
  const update = useUpdate<SendEmailNode["data"]>({ id: blockId, editable });
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id: blockId });

  return (
    <div data-testid="send-email-block-form" className="space-y-4 px-4 py-4">
      <div className="space-y-2">
        <div className="flex justify-between">
          <Label className="text-xs text-slate-300">Recipients</Label>
          {isFirstWorkflowBlock ? (
            <div className="flex justify-end text-xs text-slate-400">
              Tip: Use the {"+"} button to add inputs!
            </div>
          ) : null}
        </div>
        <WorkflowBlockInput
          nodeId={blockId}
          onChange={(value) => update({ recipients: value })}
          value={recipients}
          placeholder="example@gmail.com, example2@gmail.com..."
          className="nopan text-xs"
        />
      </div>
      <Separator />
      <div className="space-y-2">
        <Label className="text-xs text-slate-300">Subject</Label>
        <WorkflowBlockInput
          nodeId={blockId}
          onChange={(value) => update({ subject: value })}
          value={subject}
          placeholder="What is the gist?"
          className="nopan text-xs"
        />
      </div>
      <div className="space-y-2">
        <Label className="text-xs text-slate-300">Body</Label>
        <WorkflowBlockInputTextarea
          aiImprove={AI_IMPROVE_CONFIGS.sendEmail.body}
          nodeId={blockId}
          onChange={(value) => update({ body: value })}
          value={body}
          placeholder="What would you like to say?"
          className="nopan text-xs"
        />
      </div>
      <Separator />
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-slate-300">File Attachments</Label>
          <HelpTooltip content={helpTooltips["sendEmail"]["fileAttachments"]} />
        </div>
        <WorkflowBlockInput
          nodeId={blockId}
          value={fileAttachments}
          onChange={(value) => update({ fileAttachments: value })}
          disabled
          hideParameterSelect
          className="nopan text-xs"
        />
      </div>
    </div>
  );
}

export { SendEmailEditor };
