import { getClient } from "@/api/AxiosClient";
import { browserStreamingMode as buildTimeBrowserStreamingMode } from "@/util/env";
import { useQuery } from "@tanstack/react-query";

export type BrowserStreamingMode = "cdp" | "vnc";

type RuntimeConfigResponse = {
  browser_streaming_mode?: string;
  browser_streaming_label?: string;
  environment?: string;
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

export {
  browserStreamingLabel,
  normalizeBrowserStreamingMode,
  useBrowserStreamingMode,
  useRuntimeConfig,
};
