import {
  Handle,
  NodeProps,
  Position,
  useEdges,
  useNodes,
  useNodesData,
} from "@xyflow/react";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
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
import { Switch } from "@/components/ui/switch";
import { useIntegrationCatalogQuery } from "@/hooks/useIntegrationCatalogQuery";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import {
  DriveFileInput,
  DriveFileListInput,
} from "@/routes/workflows/components/DriveFileCombobox";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { cn } from "@/util/utils";

import { useCollapseContext } from "../../collapse/CollapseContext";
import { NodeBody } from "../../collapse/NodeBody";
import { useUpdate } from "../../useUpdate";
import {
  getAvailableOutputParameterKeys,
  splitEmailList,
} from "../../workflowEditorUtils";
import { type AppNode } from "..";
import {
  EmailInboxDataFields,
  EmailInboxFilterFields,
} from "../EmailInboxNode/EmailInboxEditor";
import {
  emailInboxNodeDefaultData,
  type EmailInboxNodeData,
} from "../EmailInboxNode/types";
import { GoogleSheetsReadFields } from "../GoogleSheetsReadNode/GoogleSheetsReadEditor";
import {
  googleSheetsReadNodeDefaultData,
  type GoogleSheetsReadNodeData,
} from "../GoogleSheetsReadNode/types";
import { GoogleSheetsWriteFields } from "../GoogleSheetsWriteNode/GoogleSheetsWriteEditor";
import {
  googleSheetsWriteNodeDefaultData,
  type GoogleSheetsWriteNodeData,
} from "../GoogleSheetsWriteNode/types";
import { SendEmailFields } from "../SendEmailNode/SendEmailEditor";
import {
  sendEmailNodeDefaultData,
  type SendEmailNodeData,
} from "../SendEmailNode/types";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { BuildModeOnly } from "../BuildModeOnly";
import { NodeHeader } from "../components/NodeHeader";
import { IntegrationAccountPicker } from "./IntegrationAccountPicker";
import { knownAccountBackends } from "./knownAccounts";
import {
  actionAcceptsAccount,
  editableInputs,
  fieldLabel,
  isGenericActionAvailable,
  isTemplate,
  schemaType,
  type IntegrationActionSpec,
  type IntegrationActionNode as IntegrationActionNodeType,
  type IntegrationActionNodeData,
  type IntegrationCatalogEntryWithActions,
  type JsonSchema,
} from "./types";
import { entryNeedsAccount } from "./validate";

type FieldProps = {
  nodeId: string;
  name: string;
  schema: JsonSchema;
  value: unknown;
  disabled: boolean;
  connectionId?: string;
  required?: boolean;
  onChange: (value: unknown) => void;
};

const UNSET_OPTION = "__unset__";

function toText(value: unknown): string {
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function parseJson(text: string): unknown {
  if (!text.trim()) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

// Only plain decimals become numbers; anything else (a template, "0x10", a typo) stays text for validation to report.
function parseNumber(text: string): unknown {
  if (!text.trim()) return undefined;
  return /^\s*-?(\d+\.?\d*|\.\d+)\s*$/.test(text) ? Number(text) : text;
}

type TextFieldProps = FieldProps & {
  parse: (text: string) => unknown;
  className: string;
  placeholder?: string;
};

// Local text so "1." or a half-typed object is not rewritten on every keystroke;
// a value changed elsewhere (sidebar, canvas, YAML, undo) replaces it.
function ParsedTextField({
  nodeId,
  name,
  value,
  disabled,
  onChange,
  parse,
  className,
  placeholder,
}: TextFieldProps) {
  const [text, setText] = useState(() => toText(value));
  const [seen, setSeen] = useState(value);
  if (seen !== value) {
    setSeen(value);
    if (JSON.stringify(parse(text)) !== JSON.stringify(value)) {
      setText(toText(value));
    }
  }
  return (
    <WorkflowBlockInputTextarea
      nodeId={nodeId}
      name={name}
      value={text}
      disabled={disabled}
      className={className}
      placeholder={placeholder}
      onChange={(next) => {
        setText(next);
        onChange(parse(next));
      }}
    />
  );
}

function InputField(props: FieldProps) {
  const {
    nodeId,
    name,
    schema,
    value,
    disabled,
    connectionId,
    required,
    onChange,
  } = props;
  const type = schemaType(schema);
  const driveFileList =
    type === "array" && schema.items?.format === "google_drive_file";
  if (driveFileList) {
    if (!connectionId || isTemplate(value)) {
      return (
        <WorkflowBlockInputTextarea
          nodeId={nodeId}
          name={name}
          value={
            Array.isArray(value) ? value.map(String).join("\n") : toText(value)
          }
          disabled={disabled || !connectionId}
          placeholder={
            connectionId ? undefined : "Choose a Google Drive account first"
          }
          className="nopan text-xs"
          onChange={(text) => {
            const items = text
              .split(/\r?\n/)
              .map((item) => item.trim())
              .filter(Boolean);
            onChange(items.length ? items : undefined);
          }}
        />
      );
    }
    return (
      <DriveFileListInput
        nodeId={nodeId}
        name={name}
        credentialId={connectionId}
        value={
          Array.isArray(value)
            ? value.map(String)
            : value == null || value === ""
              ? []
              : [toText(value)]
        }
        disabled={disabled}
        onChange={(items) => onChange(items.length === 0 ? undefined : items)}
      />
    );
  }
  const driveKind =
    schema.format === "google_drive_folder"
      ? "folder"
      : schema.format === "google_drive_file"
        ? "any"
        : null;
  if (driveKind) {
    if (isTemplate(value) || !connectionId) {
      return (
        <WorkflowBlockInputTextarea
          nodeId={nodeId}
          name={name}
          value={toText(value)}
          disabled={disabled || !connectionId}
          placeholder={
            connectionId ? undefined : "Choose a Google Drive account first"
          }
          className="nopan text-xs"
          onChange={(text) => onChange(text === "" ? undefined : text)}
        />
      );
    }
    return (
      <DriveFileInput
        nodeId={nodeId}
        name={name}
        credentialId={connectionId}
        kind={driveKind}
        value={toText(value)}
        disabled={disabled}
        onChange={(text) => onChange(text === "" ? undefined : text)}
      />
    );
  }
  // A templated value would show blank in a dropdown or "off" on a switch, so those show it as text.
  // Number and JSON fields keep their own text field, so typing "{{" does not swap the element mid-edit.
  const hasControl = Boolean(schema.enum) || type === "boolean";
  if (
    (type === "string" && !schema.enum) ||
    (hasControl && isTemplate(value))
  ) {
    return (
      <WorkflowBlockInputTextarea
        nodeId={nodeId}
        name={name}
        value={toText(value)}
        disabled={disabled}
        className={cn(
          "nopan text-xs",
          schema.format === "textarea" && "min-h-24",
        )}
        onChange={(text) => onChange(text === "" ? undefined : text)}
      />
    );
  }
  if (schema.enum) {
    const options = schema.enum.map(String);
    return (
      <Select
        value={value === undefined ? "" : String(value)}
        disabled={disabled}
        onValueChange={(selected) =>
          onChange(
            selected === UNSET_OPTION
              ? undefined
              : schema.enum?.[options.indexOf(selected)],
          )
        }
      >
        <SelectTrigger className="nopan text-xs" aria-label={name}>
          <SelectValue
            placeholder={
              schema.default === undefined || schema.default === null
                ? "Select"
                : `Default: ${String(schema.default)}`
            }
          />
        </SelectTrigger>
        <SelectContent>
          {required ? null : (
            <SelectItem value={UNSET_OPTION}>Not set</SelectItem>
          )}
          {options.map((option) => (
            <SelectItem key={option} value={option}>
              {option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }
  if (type === "boolean") {
    return (
      <Switch
        aria-label={name}
        // An unset required input saves nothing, so it shows off rather than its default.
        checked={(value ?? (required ? undefined : schema.default)) === true}
        disabled={disabled}
        onCheckedChange={onChange}
      />
    );
  }
  if (type === "number" || type === "integer") {
    return (
      <ParsedTextField
        {...props}
        parse={parseNumber}
        className="nopan text-xs"
      />
    );
  }
  return (
    <ParsedTextField
      {...props}
      parse={parseJson}
      className="nopan min-h-20 font-mono text-xs"
      placeholder={type === "array" ? "[]" : "{}"}
    />
  );
}

function stringInput(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function listInput(value: unknown): string {
  return Array.isArray(value)
    ? value.map(String).join(", ")
    : stringInput(value);
}

function columnMappingInput(value: unknown): string {
  if (typeof value === "string") return value;
  return value && typeof value === "object" && !Array.isArray(value)
    ? JSON.stringify(value, null, 2)
    : "";
}

function columnMappingOutput(value: unknown): unknown {
  if (typeof value !== "string" || !value.trim()) return undefined;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function updateActionInputs(
  inputs: Record<string, unknown>,
  changes: object,
  fieldNames: Record<string, string>,
  serializers: Record<string, (value: unknown) => unknown> = {},
): Record<string, unknown> {
  const next = { ...inputs };
  for (const [field, value] of Object.entries(changes)) {
    const input = fieldNames[field];
    if (!input) continue;
    const serializer = serializers[field];
    const serialized = serializer ? serializer(value) : value;
    if (serialized === undefined) delete next[input];
    else next[input] = serialized;
  }
  return next;
}

type RichActionFieldsProps = {
  action: IntegrationActionSpec;
  blockId: string;
  data: IntegrationActionNodeData;
  update: (updates: Partial<IntegrationActionNodeData>) => unknown;
};

const SHEETS_READ_FIELDS: Record<string, string> = {
  spreadsheetUrl: "spreadsheet_url",
  sheetName: "sheet_name",
  range: "range",
  hasHeaderRow: "has_header_row",
};

function GoogleSheetsReadActionFields({
  blockId,
  data,
  update,
}: RichActionFieldsProps) {
  const editorData: GoogleSheetsReadNodeData = {
    ...googleSheetsReadNodeDefaultData,
    label: data.label,
    editable: data.editable,
    credentialId: data.connectionId,
    spreadsheetUrl: stringInput(data.inputs.spreadsheet_url),
    sheetName: stringInput(data.inputs.sheet_name),
    range: stringInput(data.inputs.range),
    hasHeaderRow:
      typeof data.inputs.has_header_row === "boolean"
        ? data.inputs.has_header_row
        : googleSheetsReadNodeDefaultData.hasHeaderRow,
    parameterKeys: data.parameterKeys,
  };
  return (
    <GoogleSheetsReadFields
      blockId={blockId}
      data={editorData}
      showAccount={false}
      showParameters={false}
      update={(changes) =>
        update({
          inputs: updateActionInputs(data.inputs, changes, SHEETS_READ_FIELDS),
        })
      }
    />
  );
}

const SHEETS_WRITE_FIELDS: Record<string, string> = {
  spreadsheetUrl: "spreadsheet_url",
  sheetName: "sheet_name",
  range: "range",
  writeMode: "write_mode",
  values: "values",
  columnMapping: "column_mapping",
  createSheetIfMissing: "create_sheet_if_missing",
};

function GoogleSheetsWriteActionFields({
  blockId,
  data,
  update,
}: RichActionFieldsProps) {
  const editorData: GoogleSheetsWriteNodeData = {
    ...googleSheetsWriteNodeDefaultData,
    label: data.label,
    editable: data.editable,
    credentialId: data.connectionId,
    spreadsheetUrl: stringInput(data.inputs.spreadsheet_url),
    sheetName: stringInput(data.inputs.sheet_name),
    range: stringInput(data.inputs.range),
    writeMode: data.inputs.write_mode === "update" ? "update" : "append",
    values: stringInput(data.inputs.values),
    columnMapping: columnMappingInput(data.inputs.column_mapping),
    createSheetIfMissing: data.inputs.create_sheet_if_missing === true,
    parameterKeys: data.parameterKeys,
  };
  return (
    <GoogleSheetsWriteFields
      blockId={blockId}
      data={editorData}
      showAccount={false}
      showParameters={false}
      update={(changes) =>
        update({
          inputs: updateActionInputs(
            data.inputs,
            changes,
            SHEETS_WRITE_FIELDS,
            { columnMapping: columnMappingOutput },
          ),
        })
      }
    />
  );
}

const GMAIL_INBOX_FIELDS: Record<string, string> = {
  folder: "folder",
  prompt: "prompt",
  sender: "sender",
  subject: "subject",
  newerThanDays: "newer_than_days",
  maxResults: "max_results",
  includeBody: "include_body",
  downloadAttachments: "download_attachments",
};

function organizeAction(
  actionId: string,
): EmailInboxNodeData["organizeAction"] {
  if (actionId === "label_email") return "label";
  if (actionId === "archive_email") return "archive";
  if (actionId === "move_email") return "move";
  if (actionId === "mark_email_read") return "mark_read";
  return undefined;
}

function GmailInboxActionFields({
  action,
  blockId,
  data,
  update,
}: RichActionFieldsProps) {
  const isRead = action.id === "read_inbox";
  const editorData: EmailInboxNodeData = {
    ...emailInboxNodeDefaultData,
    label: data.label,
    editable: data.editable,
    credentialId: data.connectionId,
    credentialSource: "native",
    folder: stringInput(data.inputs.folder, "INBOX"),
    prompt: stringInput(data.inputs.prompt),
    sender: stringInput(data.inputs.sender),
    subject: stringInput(data.inputs.subject),
    newerThanDays:
      typeof data.inputs.newer_than_days === "number"
        ? data.inputs.newer_than_days
        : null,
    maxResults:
      typeof data.inputs.max_results === "number"
        ? data.inputs.max_results
        : emailInboxNodeDefaultData.maxResults,
    includeBody:
      typeof data.inputs.include_body === "boolean"
        ? data.inputs.include_body
        : emailInboxNodeDefaultData.includeBody,
    downloadAttachments: data.inputs.download_attachments === true,
    messageId: "",
    organizeAction: organizeAction(action.id),
    organizeLabel: stringInput(data.inputs.organize_label),
    parameterKeys: data.parameterKeys,
  };
  const updateFields = (changes: Partial<EmailInboxNodeData>) =>
    update({
      inputs: updateActionInputs(data.inputs, changes, GMAIL_INBOX_FIELDS),
    });
  const labelField = editableInputs(action.inputs).find(
    ([key]) => key === "organize_label",
  );
  return (
    <Accordion type="multiple" defaultValue={["data", "filters"]}>
      <AccordionItem value="data" className="border-b-0">
        <AccordionTrigger className="py-2">Data</AccordionTrigger>
        <AccordionContent className="pl-6 pr-1 pt-4">
          <EmailInboxDataFields
            blockId={blockId}
            data={editorData}
            showMessageId={false}
            update={updateFields}
          />
        </AccordionContent>
      </AccordionItem>
      <AccordionItem value="filters" className="border-b-0">
        <AccordionTrigger className="py-2">Optional Filters</AccordionTrigger>
        <AccordionContent className="space-y-3 pl-6 pr-1 pt-4">
          <EmailInboxFilterFields
            blockId={blockId}
            data={editorData}
            showReadOptions={isRead}
            update={updateFields}
          />
          {labelField ? (
            <div className="space-y-2">
              <Label className="text-xs text-slate-300">
                {fieldLabel(labelField[0], labelField[1])} *
              </Label>
              {labelField[1].description ? (
                <p className="text-xs text-muted-foreground">
                  {labelField[1].description}
                </p>
              ) : null}
              <InputField
                nodeId={blockId}
                name={labelField[0]}
                schema={labelField[1]}
                value={data.inputs.organize_label}
                disabled={!data.editable}
                required
                onChange={(value) => {
                  const inputs = { ...data.inputs };
                  if (value === undefined) delete inputs.organize_label;
                  else inputs.organize_label = value;
                  update({ inputs });
                }}
              />
            </div>
          ) : null}
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  );
}

const GMAIL_SEND_FIELDS: Record<string, string> = {
  recipients: "recipients",
  subject: "subject",
  body: "body",
  bodyFormat: "body_format",
  fileAttachments: "file_attachments",
  cc: "cc",
  bcc: "bcc",
  replyToMessageId: "reply_to_message_id",
};

const LIST_SERIALIZERS = {
  recipients: (value: unknown) => splitEmailList(stringInput(value)),
  fileAttachments: (value: unknown) => splitEmailList(stringInput(value)),
  cc: (value: unknown) => splitEmailList(stringInput(value)),
  bcc: (value: unknown) => splitEmailList(stringInput(value)),
};

function GmailSendActionFields({
  blockId,
  data,
  update,
}: RichActionFieldsProps) {
  const editorData: SendEmailNodeData = {
    ...sendEmailNodeDefaultData,
    label: data.label,
    editable: data.editable,
    transport: "gmail",
    credentialId: data.connectionId,
    credentialSource: "native",
    recipients: listInput(data.inputs.recipients),
    subject: stringInput(data.inputs.subject),
    body: stringInput(data.inputs.body),
    bodyFormat: data.inputs.body_format === "html" ? "html" : "text",
    fileAttachments: listInput(data.inputs.file_attachments),
    cc: listInput(data.inputs.cc),
    bcc: listInput(data.inputs.bcc),
    replyToMessageId: stringInput(data.inputs.reply_to_message_id),
  };
  return (
    <SendEmailFields
      blockId={blockId}
      data={editorData}
      showAccount={false}
      showTransport={false}
      update={(changes) => {
        const inputs = updateActionInputs(
          data.inputs,
          changes,
          GMAIL_SEND_FIELDS,
          LIST_SERIALIZERS,
        );
        if (stringInput(inputs.reply_to_message_id).trim()) inputs.subject = "";
        update({ inputs });
      }}
    />
  );
}

function hasRichActionFields(integrationId: string, actionId: string) {
  return (
    (integrationId === "google_sheets" &&
      (actionId === "read_rows" || actionId === "write_rows")) ||
    (integrationId === "gmail" &&
      [
        "read_inbox",
        "send_email",
        "label_email",
        "archive_email",
        "move_email",
        "mark_email_read",
      ].includes(actionId))
  );
}

function RichActionFields(props: RichActionFieldsProps) {
  const { action, data } = props;
  if (data.integrationId === "google_sheets") {
    if (action.id === "read_rows") {
      return <GoogleSheetsReadActionFields {...props} />;
    }
    if (action.id === "write_rows") {
      return <GoogleSheetsWriteActionFields {...props} />;
    }
  }
  if (data.integrationId === "gmail") {
    if (action.id === "send_email") {
      return <GmailSendActionFields {...props} />;
    }
    return <GmailInboxActionFields {...props} />;
  }
  return null;
}

function IntegrationActionEditor({ blockId }: { blockId: string }) {
  const nodeSlice = useNodesData<IntegrationActionNodeType>(blockId);
  const data = nodeSlice?.data;
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const { data: catalog, isLoading } =
    useIntegrationCatalogQuery<IntegrationCatalogEntryWithActions>();
  const update = useUpdate<IntegrationActionNodeData>({
    id: blockId,
    editable: data?.editable ?? false,
  });
  if (!data) return null;
  const entry = catalog?.find((item) => item.id === data.integrationId);
  if (!entry) {
    return isLoading ? null : (
      <p className="text-xs text-muted-foreground">
        This app is not available in your app list.
      </p>
    );
  }
  const action = entry.actions?.find((item) => item.id === data.actionId);
  const required = new Set(action?.inputs.required ?? []);
  return (
    <div className="space-y-4">
      {entryNeedsAccount(entry) ? (
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Account</Label>
          <IntegrationAccountPicker
            nodeId={blockId}
            entry={entry}
            actionKey={`${entry.id}.${data.actionId}`}
            value={data.connectionId}
            disabled={!data.editable}
            onChange={(connectionId) => update({ connectionId })}
          />
        </div>
      ) : null}
      <div className="space-y-2">
        <Label className="text-xs text-slate-300">Action</Label>
        <Select
          value={action?.id}
          disabled={!data.editable}
          onValueChange={(actionId) => {
            const next = entry.actions?.find((item) => item.id === actionId);
            const properties = next?.inputs.properties ?? {};
            const inputs = Object.fromEntries(
              Object.entries(data.inputs).filter(([key]) =>
                Object.prototype.hasOwnProperty.call(properties, key),
              ),
            );
            update({
              actionId,
              inputs,
              ...(actionAcceptsAccount(
                next,
                knownAccountBackends().get(data.connectionId),
              )
                ? {}
                : { connectionId: "" }),
            });
          }}
        >
          <SelectTrigger className="nopan text-xs" aria-label="Action">
            <SelectValue placeholder="Choose an action" />
          </SelectTrigger>
          <SelectContent>
            {(entry.actions ?? [])
              .filter(
                (item) =>
                  item.id === data.actionId || isGenericActionAvailable(item),
              )
              .map((item) => (
                <SelectItem key={item.id} value={item.id}>
                  {item.name}
                </SelectItem>
              ))}
          </SelectContent>
        </Select>
      </div>
      {!action && data.actionId ? (
        <p className="text-xs text-muted-foreground">
          This action is not available in your app list.
        </p>
      ) : null}
      {action && hasRichActionFields(entry.id, action.id) ? (
        <RichActionFields
          action={action}
          blockId={blockId}
          data={data}
          update={update}
        />
      ) : (
        (action ? editableInputs(action.inputs) : []).map(([key, schema]) => (
          <div key={`${blockId}:${key}`} className="space-y-2">
            <Label className="text-xs text-slate-300">
              {fieldLabel(key, schema)}
              {required.has(key) ? " *" : ""}
            </Label>
            {schema.description ? (
              <p className="text-xs text-muted-foreground">
                {schema.description}
              </p>
            ) : null}
            <InputField
              nodeId={blockId}
              name={key}
              schema={schema}
              value={data.inputs[key]}
              disabled={!data.editable}
              connectionId={data.connectionId}
              required={required.has(key)}
              onChange={(value) => {
                const inputs = { ...data.inputs };
                if (value === undefined) delete inputs[key];
                else inputs[key] = value;
                update({ inputs });
              }}
            />
          </div>
        ))
      )}
      {action ? (
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Parameter bindings</Label>
          <ParametersMultiSelect
            availableOutputParameters={getAvailableOutputParameterKeys(
              nodes,
              edges,
              blockId,
            )}
            parameters={data.parameterKeys}
            onParametersChange={(parameterKeys) => update({ parameterKeys })}
            disabled={!data.editable}
          />
        </div>
      ) : null}
    </div>
  );
}

function IntegrationActionNode({
  id,
  data,
}: NodeProps<IntegrationActionNodeType>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const { data: catalog } =
    useIntegrationCatalogQuery<IntegrationCatalogEntryWithActions>();
  const entry = catalog?.find((item) => item.id === data.integrationId);
  const action = entry?.actions?.find((item) => item.id === data.actionId);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRun && statusIsRunningOrQueued(workflowRun) && thisBlockIsTargetted;
  const { open } = useCollapseContext();

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
          "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-shadow motion-reduce:transition-none",
          open ? "shadow-md" : "shadow-sm",
          {
            "pointer-events-none": thisBlockIsPlaying,
            "bg-background outline outline-2 outline-ring":
              thisBlockIsTargetted,
          },
        )}
      >
        <NodeHeader
          blockLabel={label}
          blockTitle={(action?.name ?? data.actionId) || "Choose an action"}
          editable={editable}
          integrationLogo={entry?.logo}
          nodeId={id}
          totpIdentifier={null}
          totpUrl={null}
          type="integration_action"
        />
        <NodeBody>
          <BuildModeOnly renderInReadOnlyComparison={false}>
            <IntegrationActionEditor blockId={id} />
          </BuildModeOnly>
        </NodeBody>
      </div>
    </div>
  );
}

export { InputField, IntegrationActionEditor, IntegrationActionNode };
