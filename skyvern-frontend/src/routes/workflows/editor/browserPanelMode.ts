import type { BrowserStreamingMode } from "@/hooks/useRuntimeConfig";

export interface BrowserPanelLatch {
  streamTransport: BrowserStreamingMode | undefined;
  cdpRecordingEnabled: boolean;
}

export function updateBrowserPanelLatch(
  latched: BrowserPanelLatch,
  current: BrowserPanelLatch,
  isRecording: boolean,
): BrowserPanelLatch {
  return isRecording ? latched : current;
}

export function resolveCdpRecordingExfiltrate({
  cdpRecordingEnabled,
  isRecording,
  finishRequested,
}: {
  cdpRecordingEnabled: boolean;
  isRecording: boolean;
  finishRequested: boolean;
}): boolean | undefined {
  return cdpRecordingEnabled ? isRecording && !finishRequested : undefined;
}

export function resolveBrowserPanelMode({
  streamTransport,
  isRecording,
  cdpRecordingEnabled,
}: {
  streamTransport: BrowserStreamingMode | undefined;
  isRecording: boolean;
  cdpRecordingEnabled: boolean;
}): { showCdp: boolean; preferVnc: boolean } {
  return {
    showCdp: streamTransport === "cdp" && (cdpRecordingEnabled || !isRecording),
    preferVnc:
      streamTransport !== "cdp" || (!cdpRecordingEnabled && isRecording),
  };
}
