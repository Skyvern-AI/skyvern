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

async function fetchDiagnostics(): Promise<AuthDiagnosticsResponse> {
  const client = await getClient(null);
  try {
    const response = await client.get<AuthDiagnosticsResponse>(
      "/internal/auth/status",
    );
    return response.data;
  } catch (error) {
    if (axios.isAxiosError(error)) {
      const status = error.response?.status;
      // 404: the diagnostics endpoint doesn't exist (e.g. cloud). 403: it exists
      // but the backend restricts it to loopback, which a browser request to a
      // Dockerized backend can't satisfy. Neither says anything about the API
      // key, so don't surface a banner; genuine auth failures still raise it
      // via AuthIssueStore when real requests are rejected.
      if (status === 404 || status === 403) {
        return { status: "ok" };
      }
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

export { useAuthDiagnostics };
