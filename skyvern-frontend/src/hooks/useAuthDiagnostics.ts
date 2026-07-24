import { useQuery } from "@tanstack/react-query";
import axios from "axios";

import { getClient } from "@/api/AxiosClient";

export type AuthStatusValue =
  | "missing_api_key"
  | "invalid_format"
  | "invalid"
  | "expired"
  | "not_found"
  | "ok";

export type AuthDiagnosticsResponse = {
  status: AuthStatusValue;
  detail?: string;
  next_step?: string;
  fingerprint?: string;
  organization_id?: string;
  expires_at?: number;
  api_key?: string;
};

// HTTP statuses that mean "diagnostics can't run against THIS deployment" —
// not "the backend is down". Fail open ("ok") for these so we don't show the
// misleading "could not reach the diagnostics endpoint / backend not running"
// banner when the backend is clearly reachable.
//   404 -> endpoint not registered (older / non-local backend).
//   401/403 -> an auth layer or version-skewed image is in front of the
//              local-only endpoint (SKY-11308: skyvern-ui:latest calling a
//              backend that answers the diagnostics route with a 401).
// Genuine API-key problems on a forge backend come back as a 200 with an
// explicit AuthStatus (invalid/expired/not_found), and real failing requests
// are still surfaced via the AuthIssue store (request_auth_error banner), so
// failing open here never hides an actionable auth error.
const DIAGNOSTICS_UNAVAILABLE_STATUSES = new Set([401, 403, 404]);

async function fetchDiagnostics(): Promise<AuthDiagnosticsResponse> {
  const client = await getClient(null);
  try {
    const response = await client.get<AuthDiagnosticsResponse>(
      "/internal/auth/status",
    );
    return response.data;
  } catch (error) {
    const status = axios.isAxiosError(error)
      ? error.response?.status
      : undefined;
    if (status !== undefined && DIAGNOSTICS_UNAVAILABLE_STATUSES.has(status)) {
      return { status: "ok" };
    }
    throw error;
  }
}

function useAuthDiagnostics() {
  return useQuery<AuthDiagnosticsResponse, Error>({
    queryKey: ["internal", "auth", "status"],
    queryFn: fetchDiagnostics,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export { fetchDiagnostics, useAuthDiagnostics };
