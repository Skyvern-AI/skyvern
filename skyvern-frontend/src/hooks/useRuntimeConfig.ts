import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { browserStreamingMode as buildTimeBrowserStreamingMode } from "@/util/env";
import { useQuery } from "@tanstack/react-query";

export type BrowserStreamingMode = "cdp" | "vnc";

export type RuntimeConfigResponse = {
  browser_streaming_mode?: string;
  browser_streaming_label?: string;
  environment?: string;
  workflow_copilot_code_block_mode?: boolean;
  code_block_access?: boolean;
  warnings?: string[];
};

const STREAMING_MODES = new Set(["cdp", "vnc"]);

function normalizeBrowserStreamingMode(
  value: string | null | undefined,
): BrowserStreamingMode {
  const normalized = (value ?? "").trim().toLowerCase();
  return STREAMING_MODES.has(normalized)
    ? (normalized as BrowserStreamingMode)
    : "vnc";
}

function browserStreamingLabel(mode: BrowserStreamingMode) {
  return mode === "cdp" ? "Local browser streaming" : "VNC streaming";
}

function useRuntimeConfig() {
  return useQuery<RuntimeConfigResponse>({
    queryKey: ["runtimeConfig"],
    queryFn: async () => {
      const client = await getClient(null, "sans-api-v1");
      return client.get("/config/runtime").then((response) => response.data);
    },
    staleTime: 5 * 60 * 1000,
    gcTime: 30 * 60 * 1000,
    refetchOnWindowFocus: false,
    retry: 1,
  });
}

function useBrowserStreamingMode() {
  const query = useRuntimeConfig();
  const mode = normalizeBrowserStreamingMode(
    query.data?.browser_streaming_mode ?? buildTimeBrowserStreamingMode,
  );

  return {
    browserStreamingMode: mode,
    browserStreamingLabel:
      query.data?.browser_streaming_label ?? browserStreamingLabel(mode),
    runtimeConfigSource: query.data ? "backend" : "build-time-fallback",
    runtimeConfigWarnings: query.data?.warnings ?? [],
    runtimeConfigQuery: query,
  };
}

function resolveStreamTransport(
  globalMode: BrowserStreamingMode,
  sessionTransport: string | null | undefined,
): BrowserStreamingMode {
  const normalized = (sessionTransport ?? "").trim().toLowerCase();
  if (!STREAMING_MODES.has(normalized)) {
    return globalMode;
  }
  return normalized as BrowserStreamingMode;
}

function useStreamTransport(browserSessionId?: string | null) {
  const { browserStreamingMode } = useBrowserStreamingMode();
  const credentialGetter = useCredentialGetter();
  const query = useQuery<{ stream_transport?: string | null }>({
    // Deliberately the same key BrowserSession.tsx uses for its session query,
    // so react-query dedupes the fetch on pages that already load the session.
    queryKey: ["browserSession", browserSessionId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/browser_sessions/${browserSessionId}`)
        .then((response) => response.data);
    },
    enabled: Boolean(browserSessionId),
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
    retry: 1,
  });

  // Undefined until the session answers. Reporting the deployment default in the meantime is a
  // different claim from "this session streams that way", and a consumer acting on it opens a
  // stream the session may not serve, then swaps once the real answer lands.
  const pending = Boolean(browserSessionId) && query.isPending;

  return {
    streamTransport: pending
      ? undefined
      : resolveStreamTransport(
          browserStreamingMode,
          query.data?.stream_transport,
        ),
  };
}

export {
  browserStreamingLabel,
  normalizeBrowserStreamingMode,
  resolveStreamTransport,
  useBrowserStreamingMode,
  useRuntimeConfig,
  useStreamTransport,
};
