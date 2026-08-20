import { useReactFlow } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
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
  const {
    editable,
    recipients,
    subject,
    body,
    fileAttachments,
    sender,
    customSmtpHost,
    customSmtpPort,
    customSmtpUsername,
    customSmtpPassword,
  } = node.data;
  const update = useUpdate<SendEmailNode["data"]>({ id: blockId, editable });
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id: blockId });

  return (
    <div data-testid="send-email-block-form" className="space-y-4 px-4 py-4">
      <div className="space-y-2">
        <div className="flex justify-between">
          <Label className="text-xs text-tertiary-foreground">Recipients</Label>
          {isFirstWorkflowBlock ? (
            <div className="flex justify-end text-xs text-muted-foreground">
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
        <Label className="text-xs text-tertiary-foreground">Subject</Label>
        <WorkflowBlockInput
          nodeId={blockId}
          onChange={(value) => update({ subject: value })}
          value={subject}
          placeholder="What is the gist?"
          className="nopan text-xs"
        />
      </div>
      <div className="space-y-2">
        <Label className="text-xs text-tertiary-foreground">Body</Label>
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
          <Label className="text-xs text-tertiary-foreground">
            File Attachments
          </Label>
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
      <Separator />
      <Accordion type="single" collapsible>
        <AccordionItem value="advanced" className="border-b-0">
          <AccordionTrigger className="py-0">
            Advanced Settings
          </AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-1">
            <div className="space-y-4 pt-4">
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
                    Sender (From)
                  </Label>
                  <HelpTooltip content="The From address for the email. When using a custom SMTP server below, set this to an address that server is allowed to send as." />
                </div>
                <WorkflowBlockInput
                  nodeId={blockId}
                  onChange={(value) => update({ sender: value })}
                  value={sender}
                  placeholder="hello@skyvern.com"
                  className="nopan text-xs"
                />
              </div>
              <Separator />
              <div className="space-y-1">
                <Label className="text-xs text-tertiary-foreground">
                  Custom SMTP Server (Optional)
                </Label>
                <p className="text-xs text-muted-foreground">
                  Send through your own SMTP server instead of Skyvern's default
                  sender. Leave blank to use the default. Port 465 uses implicit
                  TLS; other ports use STARTTLS.
                </p>
              </div>
              <div className="space-y-2">
                <Label className="text-xs text-tertiary-foreground">
                  SMTP Host
                </Label>
                <WorkflowBlockInput
                  nodeId={blockId}
                  onChange={(value) => update({ customSmtpHost: value })}
                  value={customSmtpHost ?? ""}
                  placeholder="smtp.example.com"
                  className="nopan text-xs"
                />
              </div>
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
                    SMTP Port
                  </Label>
                  <HelpTooltip content="Numeric only. Defaults to 587 if left blank." />
                </div>
                <WorkflowBlockInput
                  nodeId={blockId}
                  onChange={(value) =>
                    update({ customSmtpPort: value.replace(/[^0-9]/g, "") })
                  }
                  value={customSmtpPort ?? ""}
                  placeholder="587"
                  className="nopan text-xs"
                />
                {customSmtpPort &&
                  (Number(customSmtpPort) < 1 ||
                    Number(customSmtpPort) > 65535) && (
                    <p className="text-xs text-destructive">
                      Port must be between 1 and 65535.
                    </p>
                  )}
              </div>
              <div className="space-y-2">
                <Label className="text-xs text-tertiary-foreground">
                  SMTP Username
                </Label>
                <WorkflowBlockInput
                  nodeId={blockId}
                  onChange={(value) => update({ customSmtpUsername: value })}
                  value={customSmtpUsername ?? ""}
                  placeholder="you@example.com"
                  className="nopan text-xs"
                />
              </div>
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
                    SMTP Password
                  </Label>
                  <HelpTooltip content="Encrypted at rest on Skyvern Cloud and on self-hosted deployments with encryption keys configured; stored as-is otherwise. For Gmail, use an App Password. You can also reference a secret parameter." />
                </div>
                <WorkflowBlockInput
                  nodeId={blockId}
                  type="password"
                  onChange={(value) => update({ customSmtpPassword: value })}
                  value={customSmtpPassword ?? ""}
                  className="nopan text-xs"
                />
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { SendEmailEditor };
