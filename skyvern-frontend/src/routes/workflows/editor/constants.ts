// nodes have 1000 Z index and we want edges above
export const REACT_FLOW_EDGE_Z_INDEX = 1001;

export const SKYVERN_DOWNLOAD_DIRECTORY = "SKYVERN_DOWNLOAD_DIRECTORY";

export const SMTP_HOST_PARAMETER_KEY = "smtp_host";
export const SMTP_PORT_PARAMETER_KEY = "smtp_port";
export const SMTP_USERNAME_PARAMETER_KEY = "smtp_username";
export const SMTP_PASSWORD_PARAMETER_KEY = "smtp_password";

export const SMTP_HOST_AWS_KEY = "SKYVERN_SMTP_HOST_AWS_SES";
export const SMTP_PORT_AWS_KEY = "SKYVERN_SMTP_PORT_AWS_SES";
export const SMTP_USERNAME_AWS_KEY = "SKYVERN_SMTP_USERNAME_SES";
export const SMTP_PASSWORD_AWS_KEY = "SKYVERN_SMTP_PASSWORD_SES";

export const EMAIL_BLOCK_SENDER = "hello@skyvern.com";

// Reserved parameters that are always available
// See: skyvern/forge/sdk/workflow/models/parameter.py RESERVED_PARAMETER_KEYS
export const GLOBAL_RESERVED_PARAMETERS = [
  { key: "current_date", description: "Current UTC date (YYYY-MM-DD format)" },
  {
    key: "workflow_run_outputs",
    description: "JSON of all block outputs collected so far",
  },
  {
    key: "workflow_run_summary",
    description: "Merged summary of all block outputs",
  },
  { key: "workflow_run_id", description: "Unique ID of the current run" },
  { key: "workflow_id", description: "The workflow's ID" },
  {
    key: "workflow_permanent_id",
    description: "The workflow's permanent ID",
  },
  { key: "workflow_title", description: "The workflow's title" },
] as const;

// Reserved parameters only available inside loop blocks
export const LOOP_RESERVED_PARAMETERS = [
  { key: "current_value", description: "The current item being iterated" },
  { key: "current_item", description: "Alias for current_value" },
  {
    key: "current_index",
    description: "Zero-based index of the current iteration",
  },
] as const;

export const BITWARDEN_CLIENT_ID_AWS_SECRET_KEY = "SKYVERN_BITWARDEN_CLIENT_ID";
export const BITWARDEN_CLIENT_SECRET_AWS_SECRET_KEY =
  "SKYVERN_BITWARDEN_CLIENT_SECRET";
export const BITWARDEN_MASTER_PASSWORD_AWS_SECRET_KEY =
  "SKYVERN_BITWARDEN_MASTER_PASSWORD";

type AiImproveConfig = {
  useCase: string;
  context: Record<string, unknown>;
};

const createAiImproveConfig = (
  block: string,
  field: string,
  extraContext: Record<string, unknown> = {},
): AiImproveConfig => ({
  useCase: `workflow_editor.${block}.${field}`,
  context: {
    block_type: block,
    field,
    ...extraContext,
  },
});

export const AI_IMPROVE_CONFIGS = {
  task: {
    navigationGoal: createAiImproveConfig("task", "navigation_goal"),
    dataExtractionGoal: createAiImproveConfig("task", "data_extraction_goal"),
    completeCriterion: createAiImproveConfig("task", "complete_criterion"),
  },
  action: {
    navigationGoal: createAiImproveConfig("action", "navigation_goal"),
    errorCodeMapping: createAiImproveConfig("action", "error_code_mapping"),
  },
  navigation: {
    navigationGoal: createAiImproveConfig("navigation", "navigation_goal"),
    completeCriterion: createAiImproveConfig(
      "navigation",
      "complete_criterion",
    ),
  },
  extraction: {
    dataExtractionGoal: createAiImproveConfig(
      "extraction",
      "data_extraction_goal",
    ),
    dataSchema: createAiImproveConfig("extraction", "data_schema"),
  },
  validation: {
    completeCriterion: createAiImproveConfig(
      "validation",
      "complete_criterion",
    ),
    terminateCriterion: createAiImproveConfig(
      "validation",
      "terminate_criterion",
    ),
  },
  login: {
    navigationGoal: createAiImproveConfig("login", "navigation_goal"),
    completeCriterion: createAiImproveConfig("login", "complete_criterion"),
    terminateCriterion: createAiImproveConfig("login", "terminate_criterion"),
  },
  fileDownload: {
    navigationGoal: createAiImproveConfig("file_download", "navigation_goal"),
    completeCriterion: createAiImproveConfig(
      "file_download",
      "complete_criterion",
    ),
  },
  taskV2: {
    prompt: createAiImproveConfig("task_v2", "prompt"),
  },
  textPrompt: {
    prompt: createAiImproveConfig("text_prompt", "prompt"),
    jsonSchema: createAiImproveConfig("text_prompt", "json_schema"),
  },
  humanInteraction: {
    instructions: createAiImproveConfig("human_interaction", "instructions"),
    positiveDescriptor: createAiImproveConfig(
      "human_interaction",
      "positive_descriptor",
    ),
    negativeDescriptor: createAiImproveConfig(
      "human_interaction",
      "negative_descriptor",
    ),
    body: createAiImproveConfig("human_interaction", "body"),
  },
  sendEmail: {
    subject: createAiImproveConfig("send_email", "subject"),
    body: createAiImproveConfig("send_email", "body"),
  },
  httpRequest: {
    body: createAiImproveConfig("http_request", "body"),
  },
} as const;
