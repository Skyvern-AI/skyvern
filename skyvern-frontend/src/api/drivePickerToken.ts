import { getClientWithFreshCredential } from "@/api/AxiosClient";
import { isAxiosError } from "axios";

type DrivePickerTokenResponse = {
  access_token: string;
  expires_at: string | null;
};

type DrivePickerErrorDetail = {
  code?: unknown;
  message?: unknown;
};

const PICKER_REFUSAL_CODES = new Set([
  "picker_owner_only",
  "picker_reconnect_required",
  "picker_token_too_broad",
]);

function pickerRefusalMessage(error: unknown): string | null {
  if (!isAxiosError<{ detail?: DrivePickerErrorDetail }>(error)) return null;
  if (error.response?.status !== 403) return null;
  const detail = error.response.data?.detail;
  if (
    !detail ||
    typeof detail.code !== "string" ||
    !PICKER_REFUSAL_CODES.has(detail.code) ||
    typeof detail.message !== "string"
  ) {
    return null;
  }
  return detail.message;
}

export async function fetchDrivePickerToken(
  credentialId: string,
): Promise<string> {
  try {
    const client = await getClientWithFreshCredential();
    const response = await client.post<DrivePickerTokenResponse>(
      "/integrations/google/drive/picker-token",
      { credential_id: credentialId },
    );
    if (!response.data.access_token) {
      throw new Error("Google did not return an access token.");
    }
    return response.data.access_token;
  } catch (error) {
    const message = pickerRefusalMessage(error);
    if (message) throw new Error(message);
    throw error;
  }
}
