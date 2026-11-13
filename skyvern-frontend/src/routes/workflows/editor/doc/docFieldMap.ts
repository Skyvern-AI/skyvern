import type { WorkflowBlockType } from "../../types/workflowTypes";
import { docDetailsText } from "./docDetailsText";

export type DocMetaIcon =
  | "credential"
  | "url"
  | "file"
  | "email"
  | "text"
  | "agent"
  | "method"
  | "range"
  | "model"
  | "errorCode";

// A field shown on the step's summary line under its title instead of in Details.
export type DocFieldMeta = {
  icon: DocMetaIcon;
  // When an unset field shows as a dashed chip in an editable Doc; otherwise it stays in Details as unset.
  // A "firstPage" field is needed only on the step that opens the run's first page, and is left out unset elsewhere.
  emptyChip: "always" | "firstPage" | "never";
};

// A block's key settings for Details, by saved (YAML) key. The first key with a value is shown; `empty` when none has one.
export type DocFieldSpec = {
  keys: readonly string[];
  label: string;
  empty: string;
  // The login's credential slot: the list's first entry, when it names a credential parameter.
  credentialsOnly?: boolean;
  // Left out when the saved block does not use the field (the editor hides it there too).
  hiddenWhen?: (saved: Record<string, unknown>) => boolean;
  // A value the editor holds but does not save, shown ahead of the saved keys.
  editorValue?: (data: Record<string, unknown>) => unknown;
  meta?: DocFieldMeta;
  // What follows "Add" on the button an editable Doc offers for the field while it is unset, when the label is too terse.
  addLabel?: string;
};

const field = (
  keys: string | readonly string[],
  label: string,
  empty: string = docDetailsText.notSet,
): DocFieldSpec => ({
  keys: typeof keys === "string" ? [keys] : keys,
  label,
  empty,
});

const onLine = (spec: DocFieldSpec, meta: DocFieldMeta): DocFieldSpec => ({
  ...spec,
  meta,
});

// Every field label Details uses for key settings.
const L = {
  credential: "Credential",
  twoFactor: "2FA",
  fileName: "File name",
  downloadTimeout: "Download timeout",
  engine: "Engine",
  maxSteps: "Max steps",
  maxRetries: "Max retries",
  errorCodes: "Error codes",
  url: "URL",
  terminateIf: "Terminate if",
  cache: "Cache disabled",
  continueOnFailure: "Continue on failure",
  nextLoopOnFailure: "Next loop on failure",
  completeIfEmpty: "Complete if empty",
  body: "Body",
  headers: "Headers",
  method: "Method",
  model: "Model",
  fileType: "File type",
  errorCode: "Error code",
  recipients: "Recipients",
  subject: "Subject",
  attachments: "Attachments",
  sender: "Sender",
  range: "Range",
  agent: "Agent",
} as const;

const pageUrl = (
  emptyChip: DocFieldMeta["emptyChip"] = "firstPage",
  addLabel = "Page URL",
): DocFieldSpec => ({
  ...onLine(field("url", L.url), { icon: "url", emptyChip }),
  addLabel,
});

// Up to four Details rows per type, ranked by how often users change the field between saved versions, then by how often it is set;
// a login's 2FA leads, and fields the Doc already shows, on the step's lines or its summary line, are left out.
// A `meta` field (the thing the step acts on or with) sits on the summary line, at most two per step in this order.
export const DOC_FIELD_MAP: Partial<
  Record<WorkflowBlockType, readonly DocFieldSpec[]>
> = {
  navigation: [
    field("engine", L.engine, "Default"),
    field("max_steps_per_run", L.maxSteps, "Default"),
    field("error_code_mapping", L.errorCodes),
    pageUrl(),
  ],
  code: [
    field("error_code_mapping", L.errorCodes),
    field("continue_on_failure", L.continueOnFailure, "No"),
  ],
  extraction: [
    pageUrl(),
    field("engine", L.engine, "Default"),
    field("max_steps_per_run", L.maxSteps, "Default"),
    field("max_retries", L.maxRetries, "0"),
    field("disable_cache", L.cache, "No"),
  ],
  login: [
    onLine(
      { ...field("parameter_keys", L.credential), credentialsOnly: true },
      { icon: "credential", emptyChip: "always" },
    ),
    field(["totp_identifier", "totp_verification_url"], L.twoFactor),
    field("error_code_mapping", L.errorCodes),
    pageUrl("always", "Login page URL"),
  ],
  action: [
    pageUrl(),
    field("engine", L.engine, "Default"),
    field("max_retries", L.maxRetries, "0"),
    field("error_code_mapping", L.errorCodes),
    field("continue_on_failure", L.continueOnFailure, "No"),
  ],
  file_download: [
    onLine(field("download_suffix", L.fileName), {
      icon: "file",
      emptyChip: "never",
    }),
    pageUrl(),
    field("error_code_mapping", L.errorCodes),
    field("engine", L.engine, "Default"),
    {
      ...field("download_timeout", L.downloadTimeout, "Default"),
      // A download from an SFTP path does not use the browser's download timeout.
      hiddenWhen: (saved) => Boolean(saved.sftp_source_path),
    },
  ],
  for_loop: [
    field("next_loop_on_failure", L.nextLoopOnFailure, "No"),
    field("complete_if_empty", L.completeIfEmpty, "No"),
    field("continue_on_failure", L.continueOnFailure, "No"),
  ],
  http_request: [
    {
      ...field("body", L.body),
      // The editor hides Body for these methods (a missing one reads as GET).
      hiddenWhen: (saved) =>
        ["GET", "HEAD", "DELETE"].includes(String(saved.method ?? "GET")),
    },
    field("headers", L.headers),
    field("continue_on_failure", L.continueOnFailure, "No"),
    onLine(field("method", L.method), { icon: "method", emptyChip: "never" }),
  ],
  task: [
    field("engine", L.engine, "Default"),
    field("terminate_criterion", L.terminateIf),
    pageUrl(),
    field("max_steps_per_run", L.maxSteps, "Default"),
  ],
  text_prompt: [
    onLine(field("model", L.model, "Default"), {
      icon: "model",
      emptyChip: "never",
    }),
  ],
  file_url_parser: [
    onLine(field("file_type", L.fileType), {
      icon: "file",
      emptyChip: "never",
    }),
    field("continue_on_failure", L.continueOnFailure, "No"),
  ],
  validation: [
    field("error_code_mapping", L.errorCodes),
    field("engine", L.engine, "Default"),
    field("next_loop_on_failure", L.nextLoopOnFailure, "No"),
  ],
  terminate: [
    onLine(field("error_code", L.errorCode), {
      icon: "errorCode",
      emptyChip: "never",
    }),
  ],
  send_email: [
    field("file_attachments", L.attachments),
    onLine(field("recipients", L.recipients), {
      icon: "email",
      emptyChip: "always",
    }),
    onLine(field("subject", L.subject), { icon: "text", emptyChip: "never" }),
    // Gmail sends from the connected account and saves an empty sender.
    {
      ...field("sender", L.sender),
      hiddenWhen: (saved) => saved.transport === "gmail",
    },
  ],
  // The title is the editor's name for the target; one loaded without it shows the target's id.
  workflow_trigger: [
    onLine(
      {
        ...field("workflow_permanent_id", L.agent),
        editorValue: (data) => data.workflowTitle,
      },
      { icon: "agent", emptyChip: "always" },
    ),
  ],
  google_sheets_read: [
    onLine(field("range", L.range), { icon: "range", emptyChip: "never" }),
  ],
  google_sheets_write: [
    onLine(field("range", L.range), { icon: "range", emptyChip: "never" }),
  ],
};
