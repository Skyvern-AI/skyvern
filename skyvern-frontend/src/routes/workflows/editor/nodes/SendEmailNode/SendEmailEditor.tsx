import { useNodesData } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";

import { CredentialSourceSelector } from "@/routes/workflows/components/CredentialSourceSelector";
import { type EmailTransport } from "@/routes/workflows/types/workflowTypes";

import {
  AI_IMPROVE_CONFIGS,
  SKYVERN_DOWNLOAD_DIRECTORY,
} from "../../constants";
import { helpTooltips } from "../../helpContent";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { EmailBodyFormatSelect } from "../components/EmailBodyFormatSelect";
import { type SendEmailNode, type SendEmailNodeData } from "./types";
import { useUpdate } from "../../useUpdate";

function SendEmailEditor({ blockId }: { blockId: string }) {
  // The sidebar mount lives outside the per-node renderer, so a useReactFlow().getNode(id)
  // snapshot does not re-render after updateNodeData commits; subscribe to the data slice instead.
  const nodeSlice = useNodesData<SendEmailNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "sendEmail") {
    return null;
  }
  return <SendEmailEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function SendEmailEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: SendEmailNodeData;
}) {
  const update = useUpdate<SendEmailNodeData>({
    id: blockId,
    editable: data.editable,
  });
  return <SendEmailFields blockId={blockId} data={data} update={update} />;
}

// Saving a workflow that sends only through Gmail removes these parameters, and
// the server declares them again for an SMTP block that does not name them.
// The Gmail trigger passes the whole email as the `email` input; `id` is its Gmail message ID.
const TRIGGER_EMAIL_ID = "{{ email.id }}";

const PLATFORM_SMTP_SECRET_FIELDS = [
  ["smtpHostSecretParameterKey", "smtp_host"],
  ["smtpPortSecretParameterKey", "smtp_port"],
  ["smtpUsernameSecretParameterKey", "smtp_username"],
  ["smtpPasswordSecretParameterKey", "smtp_password"],
] as const;

type SendEmailFieldsProps = {
  blockId: string;
  data: SendEmailNodeData;
  update: (updates: Partial<SendEmailNodeData>) => unknown;
  showTransport?: boolean;
  showAccount?: boolean;
};

function SendEmailFields({
  blockId,
  data,
  update,
  showTransport = true,
  showAccount = true,
}: SendEmailFieldsProps) {
  const {
    editable,
    recipients,
    subject,
    body,
    bodyFormat,
    fileAttachments,
    transport,
    credentialId,
    credentialSource,
    connectionId,
    replyToMessageId,
    cc,
    bcc,
  } = data;
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id: blockId });
  const isGmail = transport === "gmail";
  const isReply = isGmail && Boolean(replyToMessageId?.trim());
  const hasCustomReply = isReply && replyToMessageId !== TRIGGER_EMAIL_ID;

  // The SMTP settings stay in the node while Gmail is selected, so switching back
  // restores them; a Gmail block is saved without them.
  const changeTransport = (next: EmailTransport) => {
    if (next !== "gmail") {
      update({
        transport: next,
        credentialId: "",
        credentialSource: "native",
        connectionId: "",
        replyToMessageId: "",
        cc: "",
        bcc: "",
      });
      return;
    }
    const namedPlatformSecrets = PLATFORM_SMTP_SECRET_FIELDS.filter(
      ([field, key]) => data[field] === key,
    );
    update({
      transport: next,
      ...Object.fromEntries(
        namedPlatformSecrets.map(([field]) => [field, undefined]),
      ),
    });
  };

  return (
    <div data-testid="send-email-block-form" className="space-y-4 px-4 py-4">
      {showTransport ? (
        <div className="space-y-2">
          <Label className="text-xs text-tertiary-foreground">Send with</Label>
          <Select
            value={transport}
            onValueChange={(next) => changeTransport(next as EmailTransport)}
            disabled={!editable}
          >
            <SelectTrigger
              aria-label="Send with"
              className="nopan text-xs"
              data-testid="send-email-transport"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="smtp">SMTP</SelectItem>
              <SelectItem value="gmail">Connected Gmail account</SelectItem>
            </SelectContent>
          </Select>
        </div>
      ) : null}
      {showAccount && isGmail ? (
        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-tertiary-foreground">
              Gmail account
            </Label>
            <HelpTooltip content="The email is sent from this account. Only accounts connected for sending on the Integrations page are listed." />
          </div>
          <CredentialSourceSelector
            provider="gmail"
            purpose={data.replyToMessageId ? "reply" : "send"}
            nodeId={blockId}
            credentialId={credentialId}
            credentialSource={credentialSource ?? "native"}
            connectionId={connectionId ?? ""}
            disabled={!editable}
            onChange={(source, id) =>
              update(
                source === "native"
                  ? {
                      credentialSource: "native",
                      connectionId: "",
                      credentialId: id,
                      ...(id &&
                        credentialId &&
                        id !== credentialId && { replyToMessageId: "" }),
                    }
                  : {
                      credentialSource: "nango",
                      connectionId: id,
                      credentialId: "",
                      replyToMessageId: "",
                    },
              )
            }
          />
        </div>
      ) : null}
      {isGmail && credentialSource !== "nango" ? (
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <Label
              htmlFor={`${blockId}-reply`}
              className="text-xs text-tertiary-foreground"
            >
              Reply to the email that started this agent
            </Label>
            <Switch
              id={`${blockId}-reply`}
              checked={isReply}
              disabled={!editable}
              onCheckedChange={(checked) =>
                update({ replyToMessageId: checked ? TRIGGER_EMAIL_ID : "" })
              }
            />
          </div>
          {hasCustomReply ? (
            <div className="space-y-2">
              <Label
                htmlFor={`${blockId}-reply-to-message-id`}
                className="text-xs text-tertiary-foreground"
              >
                Message to reply to
              </Label>
              <WorkflowBlockInput
                id={`${blockId}-reply-to-message-id`}
                name="replyToMessageId"
                nodeId={blockId}
                value={replyToMessageId}
                onChange={(value) => update({ replyToMessageId: value })}
                disabled={!editable}
                className="nopan text-xs"
              />
            </div>
          ) : null}
        </div>
      ) : null}
      <Separator />
      <div className="space-y-2">
        <div className="flex justify-between">
          <Label className="text-xs text-tertiary-foreground">
            {isGmail ? "To" : "Recipients"}
          </Label>
          {isFirstWorkflowBlock ? (
            <div className="flex justify-end text-xs text-muted-foreground">
              Tip: Use the {"+"} button to add inputs!
            </div>
          ) : null}
        </div>
        <WorkflowBlockInput
          name="recipients"
          nodeId={blockId}
          onChange={(value) => update({ recipients: value })}
          value={recipients}
          placeholder={
            isReply
              ? "Sender of the original email (leave empty)"
              : "example@gmail.com, example2@gmail.com..."
          }
          className="nopan text-xs"
        />
      </div>
      {isGmail ? (
        <>
          <div className="space-y-2">
            <Label className="text-xs text-tertiary-foreground">Cc</Label>
            <WorkflowBlockInput
              name="cc"
              nodeId={blockId}
              onChange={(value) => update({ cc: value })}
              value={cc}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <Label className="text-xs text-tertiary-foreground">Bcc</Label>
            <WorkflowBlockInput
              name="bcc"
              nodeId={blockId}
              onChange={(value) => update({ bcc: value })}
              value={bcc}
              className="nopan text-xs"
            />
          </div>
        </>
      ) : null}
      <Separator />
      <div className="space-y-2">
        <Label className="text-xs text-tertiary-foreground">Subject</Label>
        <WorkflowBlockInput
          name="subject"
          nodeId={blockId}
          onChange={(value) => update({ subject: value })}
          value={isReply ? "" : subject}
          disabled={!editable || isReply}
          hideParameterSelect={isReply}
          placeholder={
            isReply
              ? "Re: <original subject>"
              : "Your Run is Finished {{workflow_run_id}}"
          }
          className="nopan text-xs"
        />
      </div>
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label className="text-xs text-tertiary-foreground">Body</Label>
          <EmailBodyFormatSelect
            value={bodyFormat}
            onChange={(next) => update({ bodyFormat: next })}
            disabled={!editable}
          />
        </div>
        <WorkflowBlockInputTextarea
          name="body"
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
          <HelpTooltip
            content={
              isGmail
                ? "Comma-separated files to attach: file paths from this run, uploaded files or file URLs. Folders and the download directory are not attached."
                : helpTooltips["sendEmail"]["fileAttachments"]
            }
          />
        </div>
        {isGmail ? (
          <WorkflowBlockInput
            name="fileAttachments"
            nodeId={blockId}
            value={
              fileAttachments === SKYVERN_DOWNLOAD_DIRECTORY
                ? ""
                : fileAttachments
            }
            onChange={(value) => update({ fileAttachments: value })}
            className="nopan text-xs"
          />
        ) : (
          <WorkflowBlockInput
            name="fileAttachments"
            nodeId={blockId}
            value={fileAttachments}
            onChange={(value) => update({ fileAttachments: value })}
            disabled
            hideParameterSelect
            className="nopan text-xs"
          />
        )}
      </div>
      {isGmail ? null : <SmtpAdvancedSettings blockId={blockId} data={data} />}
    </div>
  );
}

function SmtpAdvancedSettings({
  blockId,
  data,
}: {
  blockId: string;
  data: SendEmailNode["data"];
}) {
  const {
    editable,
    sender,
    customSmtpHost,
    customSmtpPort,
    customSmtpUsername,
    customSmtpPassword,
  } = data;
  const update = useUpdate<SendEmailNode["data"]>({ id: blockId, editable });

  return (
    <>
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
                  name="sender"
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
                  name="customSmtpHost"
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
                  name="customSmtpPort"
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
                  name="customSmtpUsername"
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
                  name="customSmtpPassword"
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
    </>
  );
}

export { SendEmailEditor, SendEmailFields };
