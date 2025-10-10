import { useQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";

const isDevMode = import.meta.env.MODE === "development";

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
  const response = await client.get<AuthDiagnosticsResponse>(
    "/internal/auth/status",
  );
  return response.data;
}

function useAuthDiagnostics() {
  return useQuery<AuthDiagnosticsResponse, Error>({
    queryKey: ["internal", "auth", "status"],
    queryFn: fetchDiagnostics,
    enabled: isDevMode,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export { useAuthDiagnostics };
