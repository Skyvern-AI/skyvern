export const GOOGLE_SHEETS_DATA_SCOPE =
  "https://www.googleapis.com/auth/spreadsheets";

export const GOOGLE_SHEETS_BLOCK_REQUIRED_SCOPES = [
  GOOGLE_SHEETS_DATA_SCOPE,
] as const;

export const GOOGLE_SHEETS_REQUIRED_SCOPES = [
  GOOGLE_SHEETS_DATA_SCOPE,
  "https://www.googleapis.com/auth/drive.metadata.readonly",
] as const;

export const GOOGLE_GMAIL_REQUIRED_SCOPES = [
  "https://www.googleapis.com/auth/gmail.readonly",
] as const;

export const GOOGLE_GMAIL_SEND_REQUIRED_SCOPES = [
  "https://www.googleapis.com/auth/gmail.send",
] as const;

export const GOOGLE_GMAIL_REPLY_REQUIRED_SCOPES = [
  ...GOOGLE_GMAIL_REQUIRED_SCOPES,
  ...GOOGLE_GMAIL_SEND_REQUIRED_SCOPES,
] as const;

export const GOOGLE_DRIVE_FILE_SCOPE =
  "https://www.googleapis.com/auth/drive.file";

const NARROW_DRIVE_ALLOWED_SCOPES: ReadonlySet<string> = new Set([
  GOOGLE_DRIVE_FILE_SCOPE,
  "openid",
  "email",
  "https://www.googleapis.com/auth/userinfo.email",
]);

// Strict on purpose: any scope outside this set makes the grant not narrow.
export function isNarrowDriveGrant(scopes: readonly string[]): boolean {
  return (
    scopes.includes(GOOGLE_DRIVE_FILE_SCOPE) &&
    scopes.every((scope) => NARROW_DRIVE_ALLOWED_SCOPES.has(scope))
  );
}

export const GOOGLE_DRIVE_REQUIRED_SCOPES = [
  "https://www.googleapis.com/auth/drive",
] as const;

// What a Drive step requires; GOOGLE_REQUIRED_SCOPE_RULES says which grants meet it.
export const GOOGLE_DRIVE_STEP_SCOPES = [GOOGLE_DRIVE_FILE_SCOPE] as const;

export function grantsDriveSteps(scopes: readonly string[]): boolean {
  return (
    GOOGLE_DRIVE_REQUIRED_SCOPES.some((scope) => scopes.includes(scope)) ||
    isNarrowDriveGrant(scopes)
  );
}

// Older Sheets connections hold the narrow Drive scope beside their Sheets
// scopes, so holding it must not make them Drive accounts: a requirement for a
// scope listed here is met by its rule, not by membership.
export const GOOGLE_REQUIRED_SCOPE_RULES: ReadonlyMap<
  string,
  (scopes: readonly string[]) => boolean
> = new Map([[GOOGLE_DRIVE_FILE_SCOPE, grantsDriveSteps]]);

export type GmailScopeProfile = "gmail" | "gmail_send" | "gmail_read_send";

export function gmailScopeProfile(capabilities: {
  read: boolean;
  send: boolean;
}): GmailScopeProfile | null {
  if (capabilities.read && capabilities.send) {
    return "gmail_read_send";
  }
  if (capabilities.read) {
    return "gmail";
  }
  return capabilities.send ? "gmail_send" : null;
}
