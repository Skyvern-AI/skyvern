import type { WorkflowBlockType } from "../../types/workflowTypes";
import { docDetailsText } from "./docDetailsText";

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

// Up to four per type, ranked by how often users change the field between saved versions, then by how often it is set;
// a login's credential and 2FA and a file download's file name lead, and fields the Doc already shows are left out.
export const DOC_FIELD_MAP: Partial<
  Record<WorkflowBlockType, readonly DocFieldSpec[]>
> = {
  navigation: [
    field("engine", L.engine, "Default"),
    field("max_steps_per_run", L.maxSteps, "Default"),
    field("error_code_mapping", L.errorCodes),
    field("url", L.url),
  ],
  code: [
    field("error_code_mapping", L.errorCodes),
    field("continue_on_failure", L.continueOnFailure, "No"),
  ],
  extraction: [
    field("engine", L.engine, "Default"),
    field("max_steps_per_run", L.maxSteps, "Default"),
    field("max_retries", L.maxRetries, "0"),
    field("disable_cache", L.cache, "No"),
  ],
  login: [
    { ...field("parameter_keys", L.credential), credentialsOnly: true },
    field(["totp_identifier", "totp_verification_url"], L.twoFactor),
    field("error_code_mapping", L.errorCodes),
    field("url", L.url),
  ],
  action: [
    field("engine", L.engine, "Default"),
    field("max_retries", L.maxRetries, "0"),
    field("error_code_mapping", L.errorCodes),
    field("continue_on_failure", L.continueOnFailure, "No"),
  ],
  file_download: [
    field("download_suffix", L.fileName),
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
    field("method", L.method),
  ],
  task: [
    field("engine", L.engine, "Default"),
    field("terminate_criterion", L.terminateIf),
    field("url", L.url),
    field("max_steps_per_run", L.maxSteps, "Default"),
  ],
  text_prompt: [field("model", L.model, "Default")],
  file_url_parser: [
    field("file_type", L.fileType),
    field("continue_on_failure", L.continueOnFailure, "No"),
  ],
  validation: [
    field("error_code_mapping", L.errorCodes),
    field("engine", L.engine, "Default"),
    field("next_loop_on_failure", L.nextLoopOnFailure, "No"),
  ],
  terminate: [field("error_code", L.errorCode)],
  send_email: [
    field("file_attachments", L.attachments),
    field("recipients", L.recipients),
    field("subject", L.subject),
    // Gmail sends from the connected account and saves an empty sender.
    {
      ...field("sender", L.sender),
      hiddenWhen: (saved) => saved.transport === "gmail",
    },
  ],
  // The title is the editor's name for the target; one loaded without it shows the target's id.
  workflow_trigger: [
    {
      ...field("workflow_permanent_id", L.agent),
      editorValue: (data) => data.workflowTitle,
    },
  ],
  google_sheets_read: [field("range", L.range)],
  google_sheets_write: [field("range", L.range)],
};
