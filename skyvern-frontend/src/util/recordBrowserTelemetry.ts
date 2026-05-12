import posthog from "posthog-js";

let lastProcessedAtMs: number | null = null;
let lastRecordingGeneratedBlockCount = 0;

export function captureRecordBrowser(
  event: string,
  properties?: Record<string, unknown>,
): void {
  try {
    posthog.capture(event, properties);
  } catch {
    // PostHog may be unavailable in tests or before init.
  }
}

export function markRecordBrowserProcessed(blockCount: number): void {
  if (blockCount <= 0) {
    lastProcessedAtMs = null;
    lastRecordingGeneratedBlockCount = 0;
    return;
  }
  lastProcessedAtMs = Date.now();
  lastRecordingGeneratedBlockCount = blockCount;
}

export function captureRecordBrowserUndoAfterRecordingIfRecent(
  nodesRemovedCount: number,
): void {
  if (lastProcessedAtMs === null) {
    return;
  }

  if (Date.now() - lastProcessedAtMs > 60_000) {
    lastProcessedAtMs = null;
    lastRecordingGeneratedBlockCount = 0;
    return;
  }

  if (lastRecordingGeneratedBlockCount === 0) {
    return;
  }

  if (nodesRemovedCount <= 0) {
    return;
  }

  captureRecordBrowser("record_browser.undo_after_recording", {
    nodes_removed_count: nodesRemovedCount,
    recording_generated_block_count: lastRecordingGeneratedBlockCount,
  });
}
