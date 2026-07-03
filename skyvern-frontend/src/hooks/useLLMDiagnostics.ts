import { useQuery } from "@tanstack/react-query";
import axios from "axios";

import { getClient } from "@/api/AxiosClient";

export type LLMDiagnosticsStatusValue = "ok" | "setup_required";

export type LLMConfigIssue = {
  llm_key: string;
  missing_env_vars: string[];
  detail: string;
};

export type LLMDiagnosticsResponse = {
  status: LLMDiagnosticsStatusValue;
  default_llm_key: string;
  has_server_configured_llm: boolean;
  custom_llm_count: number;
  issues: LLMConfigIssue[];
  detail?: string;
  next_step?: string;
};

export const llmDiagnosticsQueryKey = ["internal", "llms", "status"] as const;

async function fetchDiagnostics(): Promise<LLMDiagnosticsResponse> {
  const client = await getClient(null);
  try {
    const response = await client.get<LLMDiagnosticsResponse>(
      "/internal/llms/status",
    );
    return response.data;
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 404) {
      return {
        status: "ok",
        default_llm_key: "",
        has_server_configured_llm: true,
        custom_llm_count: 0,
        issues: [],
      };
    }
    throw error;
  }
}

function useLLMDiagnostics() {
  return useQuery<LLMDiagnosticsResponse, Error>({
    queryKey: llmDiagnosticsQueryKey,
    queryFn: fetchDiagnostics,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export { useLLMDiagnostics };
