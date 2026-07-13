import { useEdges, useNodes, useNodesData } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Input } from "@/components/ui/input";
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
import { GoogleOAuthCredentialSelector } from "@/routes/workflows/components/GoogleOAuthCredentialSelector";
import {
  MicrosoftOAuthCredentialSelector,
  MICROSOFT_MAIL_REQUIRED_SCOPES,
} from "@/routes/workflows/components/MicrosoftOAuthCredentialSelector";
import { GOOGLE_GMAIL_REQUIRED_SCOPES } from "@/util/googleScopes";

import { helpTooltips } from "../../helpContent";
import { containsJinjaReference } from "../../jinjaReferences";
import { useUpdate } from "../../useUpdate";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { type AppNode } from "..";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import {
  type EmailClient,
  type EmailInboxNode,
  type EmailInboxNodeData,
} from "./types";

function parseNullablePositiveInteger(value: string): number | null {
  if (!value.trim()) {
    return null;
  }
  const parsed = Number.parseInt(value, 10);
  return Number.isNaN(parsed) ? null : parsed;
}

function parsePositiveInteger(value: string, fallback: number): number {
  const parsed = Number.parseInt(value, 10);
  return Number.isNaN(parsed) ? fallback : parsed;
}

function getTemplateParameterKey(value: string): string | null {
  const match = /^\s*\{\{\s*([A-Za-z_]\w*)\s*\}\}\s*$/.exec(value);
  return match?.[1] ?? null;
}

function FieldLabel({
  field,
  label,
  fallback,
}: {
  field: keyof (typeof helpTooltips)["email_inbox"];
  label: string;
  fallback: string;
}) {
  return (
    <div className="flex gap-2">
      <Label className="text-xs text-slate-300">{label}</Label>
      <HelpTooltip content={helpTooltips["email_inbox"]?.[field] ?? fallback} />
    </div>
  );
}

function EmailInboxEditor({ blockId }: { blockId: string }) {
  const nodeSlice = useNodesData<EmailInboxNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "emailInbox") {
    return null;
  }
  return <EmailInboxEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function EmailInboxEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: EmailInboxNodeData;
}) {
  const { editable } = data;
  const update = useUpdate<EmailInboxNodeData>({ id: blockId, editable });
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );

  const updateEmailClient = (emailClient: EmailClient) => {
    update({ emailClient, credentialId: "" });
  };

  const updateCredentialId = (credentialId: string) => {
    const parameterKey = getTemplateParameterKey(credentialId);
    const previousParameterKey = getTemplateParameterKey(data.credentialId);
    const previousParameterKeyIsUsedElsewhere = previousParameterKey
      ? [data.folder, data.prompt, data.sender, data.subject].some((value) =>
          containsJinjaReference(value, previousParameterKey),
        )
      : false;
    const parameterKeys = previousParameterKeyIsUsedElsewhere
      ? [...data.parameterKeys]
      : data.parameterKeys.filter((key) => key !== previousParameterKey);
    if (parameterKey && !parameterKeys.includes(parameterKey)) {
      parameterKeys.push(parameterKey);
    }
    update({
      credentialId,
      parameterKeys,
    });
  };

  return (
    <div data-testid="email-inbox-block-form" className="space-y-4">
      <div className="space-y-3">
        <div className="text-xs font-medium uppercase tracking-wider text-slate-400">
          Connection
        </div>

        <div className="space-y-2">
          <FieldLabel
            field="emailClient"
            label="Email Client"
            fallback="The email provider to read from."
          />
          <Select
            value={data.emailClient}
            onValueChange={(value) => updateEmailClient(value as EmailClient)}
          >
            <SelectTrigger className="nopan text-xs">
              <SelectValue placeholder="Select an email client" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="gmail">Gmail</SelectItem>
              <SelectItem value="outlook">Outlook</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-2">
          <FieldLabel
            field="credentialId"
            label={
              data.emailClient === "gmail"
                ? "Google Account"
                : "Microsoft Account"
            }
            fallback="The connected account used to read email."
          />
          {data.emailClient === "gmail" ? (
            <GoogleOAuthCredentialSelector
              nodeId={blockId}
              value={data.credentialId}
              onChange={updateCredentialId}
              requiredScopes={GOOGLE_GMAIL_REQUIRED_SCOPES}
            />
          ) : (
            <MicrosoftOAuthCredentialSelector
              nodeId={blockId}
              value={data.credentialId}
              onChange={updateCredentialId}
              requiredScopes={MICROSOFT_MAIL_REQUIRED_SCOPES}
            />
          )}
        </div>
      </div>

      <Separator />

      <Accordion type="multiple" defaultValue={["data", "filters"]}>
        <AccordionItem value="data" className="border-b-0">
          <AccordionTrigger className="py-2">Data</AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-4">
            <div className="space-y-3">
              <div className="space-y-2">
                <FieldLabel
                  field="folder"
                  label="Folder"
                  fallback="Gmail label e.g. INBOX / Outlook folder e.g. inbox."
                />
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(next) => update({ folder: next })}
                  value={data.folder}
                  placeholder="INBOX"
                  className="nopan text-xs"
                />
              </div>

              <div className="space-y-2">
                <FieldLabel
                  field="prompt"
                  label="Match prompt"
                  fallback="Describe which emails to keep. Leave blank to keep all."
                />
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(next) => update({ prompt: next })}
                  value={data.prompt}
                  placeholder="Describe which emails to keep. Leave blank to keep all."
                  className="nopan text-xs"
                />
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>

        <AccordionItem value="filters" className="border-b-0">
          <AccordionTrigger className="py-2">Optional Filters</AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-4">
            <div className="space-y-3">
              <div className="space-y-2">
                <FieldLabel
                  field="sender"
                  label="Sender"
                  fallback="Only include emails from this sender."
                />
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(next) => update({ sender: next })}
                  value={data.sender}
                  placeholder="sender@example.com"
                  className="nopan text-xs"
                />
              </div>

              <div className="space-y-2">
                <FieldLabel
                  field="subject"
                  label="Subject"
                  fallback="Only include emails matching this subject."
                />
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(next) => update({ subject: next })}
                  value={data.subject}
                  placeholder="Subject contains..."
                  className="nopan text-xs"
                />
              </div>

              <div className="space-y-2">
                <FieldLabel
                  field="newerThanDays"
                  label="Newer Than Days"
                  fallback="Only include emails newer than this many days."
                />
                <Input
                  type="number"
                  min={1}
                  value={data.newerThanDays ?? ""}
                  onChange={(event) =>
                    update({
                      newerThanDays: parseNullablePositiveInteger(
                        event.currentTarget.value,
                      ),
                    })
                  }
                  placeholder="Any age"
                  className="nopan text-xs"
                />
              </div>

              <div className="space-y-2">
                <FieldLabel
                  field="maxResults"
                  label="Max Results"
                  fallback="Maximum number of emails to return."
                />
                <Input
                  type="number"
                  min={1}
                  value={data.maxResults}
                  onChange={(event) =>
                    update({
                      maxResults: parsePositiveInteger(
                        event.currentTarget.value,
                        data.maxResults,
                      ),
                    })
                  }
                  className="nopan text-xs"
                />
              </div>

              <div className="flex items-center justify-between">
                <FieldLabel
                  field="includeBody"
                  label="Include Body"
                  fallback="Include email body text in matching results."
                />
                <Switch
                  checked={data.includeBody}
                  onCheckedChange={(checked) =>
                    update({ includeBody: checked })
                  }
                />
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>

        <AccordionItem value="advanced" className="border-b-0">
          <AccordionTrigger className="py-2">
            Advanced Settings
          </AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-4">
            <ParametersMultiSelect
              availableOutputParameters={outputParameterKeys}
              parameters={data.parameterKeys}
              onParametersChange={(parameterKeys) => update({ parameterKeys })}
            />
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { EmailInboxEditor };
