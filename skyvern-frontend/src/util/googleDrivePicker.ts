import { isTemplateExpression } from "@/util/googleSheetsUrl";

function readEnv(value: string | undefined): string {
  return value?.trim() ?? "";
}

// Empty until a picker browser key exists in the same Google Cloud project as the OAuth client.
const drivePickerConfig = {
  apiKey: readEnv(import.meta.env.VITE_GOOGLE_PICKER_API_KEY),
  clientId: readEnv(import.meta.env.VITE_GOOGLE_PICKER_CLIENT_ID),
  appId: readEnv(import.meta.env.VITE_GOOGLE_PICKER_APP_ID),
};

const drivePickerConfigured = Boolean(
  drivePickerConfig.apiKey &&
  drivePickerConfig.clientId &&
  drivePickerConfig.appId,
);

// The picker needs a short-lived, credential-scoped access token in the browser, which is an
// open security decision. Until fetchDrivePickerToken mints one, the picker stays unavailable.
const drivePickerTokenFlowExists = false;

const drivePickerUnavailableReason: string | null = !drivePickerConfigured
  ? "Not set up yet: this environment has no Google picker key."
  : drivePickerTokenFlowExists
    ? null
    : "Not available yet: opening Drive from Skyvern is still being set up.";

async function fetchDrivePickerToken(credentialId: string): Promise<string> {
  void credentialId;
  throw new Error("Choosing files in Drive is not available yet.");
}

// Publish-to-web Doc links (/document/d/e/<public-id>) are not Drive file ids.
const DRIVE_ID_IN_URL =
  /\/(?:file|document|spreadsheets|presentation)\/(?:u\/\d+\/)?d\/(?!e(?:\/|$))([A-Za-z0-9_-]+)|\/folders\/([A-Za-z0-9_-]+)/;
const BARE_DRIVE_ID = /^[A-Za-z0-9_-]{20,}$/;

function driveIdFromValue(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed || isTemplateExpression(trimmed)) return null;
  if (BARE_DRIVE_ID.test(trimmed)) return trimmed;
  const match = trimmed.match(DRIVE_ID_IN_URL);
  return match?.[1] ?? match?.[2] ?? null;
}

function driveItemUrl(kind: "file" | "folder", id: string): string {
  return kind === "folder"
    ? `https://drive.google.com/drive/folders/${id}`
    : `https://drive.google.com/file/d/${id}/view`;
}

export {
  drivePickerConfig,
  drivePickerUnavailableReason,
  fetchDrivePickerToken,
  driveIdFromValue,
  driveItemUrl,
};
