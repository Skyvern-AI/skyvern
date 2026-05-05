import { AxiosError } from "axios";

export type SheetsBlockType = "google_sheets_read" | "google_sheets_write";

export async function hashSpreadsheetId(
  spreadsheetId: string,
): Promise<string | null> {
  if (!spreadsheetId) return null;
  if (typeof crypto === "undefined" || !crypto.subtle) return null;
  try {
    const bytes = new TextEncoder().encode(spreadsheetId);
    const digest = await crypto.subtle.digest("SHA-1", bytes);
    return Array.from(new Uint8Array(digest))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("")
      .slice(0, 12);
  } catch {
    return null;
  }
}

export function describeAxiosError(error: unknown): {
  error_code: string;
  http_status: number | null;
  error_message: string;
} {
  if (error instanceof AxiosError) {
    const status = error.response?.status ?? null;
    const data = error.response?.data as
      | { error?: string; detail?: string; code?: string }
      | undefined;
    const code =
      data?.code ??
      data?.error ??
      (status ? `http_${status}` : (error.code ?? "axios_error"));
    return {
      error_code: code,
      http_status: status,
      error_message: data?.detail ?? error.message,
    };
  }
  if (error instanceof Error) {
    return {
      error_code: "client_error",
      http_status: null,
      error_message: error.message,
    };
  }
  return {
    error_code: "unknown",
    http_status: null,
    error_message: "",
  };
}
