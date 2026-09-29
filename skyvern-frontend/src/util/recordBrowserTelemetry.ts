import posthog from "posthog-js";

let lastProcessedAtMs: number | null = null;
let lastRecordingGeneratedBlockCount = 0;

type RecordBrowserContext = {
  recording_attempt_id?: string;
  workflow_permanent_id?: string;
  browser_session_id?: string;
};

// Live for the current attempt only; events that can fire after it ends snapshot
// it via getRecordBrowserContext. builder.* events bypass this helper and pass
// recording ids explicitly.
let recordBrowserContext: RecordBrowserContext = {};
let lastProcessedContext: RecordBrowserContext = {};

export function setRecordBrowserContext(context: RecordBrowserContext): void {
  recordBrowserContext = context;
}

export function getRecordBrowserContext(): RecordBrowserContext {
  return recordBrowserContext;
}

export function captureRecordBrowser(
  event: string,
  properties?: Record<string, unknown>,
): void {
  try {
    posthog.capture(event, { ...recordBrowserContext, ...properties });
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
  lastProcessedContext = recordBrowserContext;
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
    ...lastProcessedContext,
    nodes_removed_count: nodesRemovedCount,
    recording_generated_block_count: lastRecordingGeneratedBlockCount,
  });
}
