import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

type Props = {
  debugSessionId?: string;
};

const debugSessionStatuses = ["created", "completed"] as const;

type DebugSessionStatus = (typeof debugSessionStatuses)[number];

interface DebugSession {
  debug_session_id: string;
  browser_session_id: string;
  vnc_streaming_supported: boolean | null;
  workflow_permanent_id: string | null;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
  status: DebugSessionStatus;
}

interface DebugSessionRun {
  ai_fallback: boolean | null;
  block_label: string;
  browser_session_id: string;
  code_gen: boolean | null;
  debug_session_id: string;
  failure_reason: string | null;
  output_parameter_id: string;
  run_with: string | null;
  script_run_id: string | null;
  status: string;
  workflow_id: string;
  workflow_permanent_id: string;
  workflow_run_id: string;
  created_at: string;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

interface DebugSessionRuns {
  debug_session: DebugSession;
  runs: DebugSessionRun[];
}

function useDebugSessionRunsQuery({ debugSessionId }: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<DebugSessionRuns>({
    queryKey: ["debug-session-runs", debugSessionId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const result = await client
        .get(`/debug-session/${debugSessionId}/runs`)
        .then((response) => response.data);
      return result;
    },
    enabled: !!debugSessionId,
  });
}

export {
  useDebugSessionRunsQuery,
  type DebugSession,
  type DebugSessionRun,
  type DebugSessionStatus,
};
