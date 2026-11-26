import { fetchDrivePickerToken } from "@/api/drivePickerToken";
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

const drivePickerUnavailableReason: string | null = drivePickerConfigured
  ? null
  : "Not set up yet: this environment has no Google picker key.";

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

export {
  drivePickerConfig,
  drivePickerConfigured,
  drivePickerUnavailableReason,
  fetchDrivePickerToken,
  driveIdFromValue,
};
