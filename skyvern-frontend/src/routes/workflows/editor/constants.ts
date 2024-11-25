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

export const commonHelpTooltipContent = {
  maxRetries:
    "Specify how many times you would like a task to retry upon failure.",
  maxStepsOverride:
    "Specify the maximum number of steps a task can take in total.",
  completeOnDownload:
    "Allow Skyvern to auto-complete the task when it downloads a file.",
  fileSuffix:
    "A file suffix that's automatically added to all downloaded files.",
  errorCodeMapping:
    "Knowing about why a task terminated can be important, specify error messages here.",
  totpVerificationUrl:
    "If you have an internal system for storing TOTP codes, link the endpoint here.",
  totpIdentifier:
    "If you are running multiple tasks or workflows at once, you will need to give the task an identifier to know that this TOTP goes with this task.",
  continueOnFailure:
    "Allow the workflow to continue if it encounters a failure.",
  cacheActions: "Cache the actions of this task.",
} as const;

export const commonFieldPlaceholders = {
  url: "https://",
  navigationGoal: 'Input text into "Name" field.',
  maxRetries: "Default: 3",
  maxStepsOverride: "Default: 10",
  downloadSuffix: "Add an ID for downloaded files",
  totpVerificationUrl: "Provide your 2FA endpoint",
  totpIdentifier: "Add an ID that links your TOTP to the task",
} as const;
