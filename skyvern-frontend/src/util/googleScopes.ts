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

export const GOOGLE_DRIVE_REQUIRED_SCOPES = [
  "https://www.googleapis.com/auth/drive",
] as const;

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
