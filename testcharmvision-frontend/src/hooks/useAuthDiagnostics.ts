import { useQuery } from "@tanstack/react-query";
import axios from "axios";

import { getClient } from "@/api/AxiosClient";

export type AuthStatusValue =
  | "missing_env"
  | "invalid_format"
  | "invalid"
  | "expired"
  | "not_found"
  | "ok";

export type AuthDiagnosticsResponse = {
  status: AuthStatusValue;
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
    if (axios.isAxiosError(error) && error.response?.status === 404) {
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

export { useAuthDiagnostics };
