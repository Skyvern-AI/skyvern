import { create } from "zustand";

import { captureRecordBrowser } from "@/util/recordBrowserTelemetry";

const EVENT_CAPTURED_SAMPLE_RATE = 0.1;

/**
 * example: {
 *  'targetInfo': {
 *    'targetId': '8B698E27F1F32372718DA73DCA0C5944',
 *    'type': 'page',
 *    'title': 'New Tab',
 *    'url': 'chrome://newtab/',
 *    'attached': True,
 *    'canAccessOpener': False,
 *    'browserContextId': 'FD13D5C556E681BB49AEED0AB2CA1972',
 * }
 */
export interface ExfiltratedEventCdpParams {
  targetInfo: {
    attached?: boolean;
    browserContextId?: string;
    canAccessOpener?: boolean;
    targetId?: string;
    title?: string;
    type?: string;
    url?: string;
  };
}

export interface ExfiltratedEventConsoleParams {
  type: string;
  url: string;
  timestamp: number;
  target: {
    className?: string;
    id?: string;
    innerText?: string;
    tagName?: string;
    text: string[];
    value?: string;
  };
  inputValue?: string;
  mousePosition: {
    xa: number | null;
    ya: number | null;
    xp: number | null;
    yp: number | null;
  };
  key?: string;
  code?: string;
  activeElement: {
    tagName?: string;
    id?: string;
    className?: string;
    boundingRect?: {
      x: number;
      y: number;
      width: number;
      height: number;
      top: number;
      right: number;
      bottom: number;
      left: number;
    } | null;
    scroll?: {
      scrollTop: number;
      scrollLeft: number;
      scrollHeight: number;
      scrollWidth: number;
      clientHeight: number;
      clientWidth: number;
    } | null;
  };
  window: {
    width: number;
    height: number;
    scrollX: number;
    scrollY: number;
  };
}

export interface MessageInExfiltratedCdpEvent {
  kind: "exfiltrated-event";
  event_name: string;
  params: ExfiltratedEventCdpParams;
  source: "cdp";
  timestamp: number;
}

export interface MessageInExfiltratedConsoleEvent {
  kind: "exfiltrated-event";
  event_name: string;
  params: ExfiltratedEventConsoleParams;
  source: "console";
  timestamp: number;
}

export type MessageInExfiltratedEvent =
  | MessageInExfiltratedCdpEvent
  | MessageInExfiltratedConsoleEvent;

export type RecordingActionKind =
  | "click"
  | "hover"
  | "input_text"
  | "url_change"
  | "wait";

/**
 * Mirrors `RecordingDraftStep` in skyvern/services/browser_recording/types.py.
 */
export interface RecordingDraftStep {
  step_id: string;
  action_kind: RecordingActionKind;
  block_type: "action" | "goto_url" | "wait";
  label: string;
  title?: string | null;
  navigation_goal?: string | null;
  url?: string | null;
  wait_sec?: number | null;
  status: "interpreting" | "ready";
  editable_fields: Array<string>;
  parameters: Array<Record<string, unknown>>;
  parameter_keys: Array<string>;
  timestamp_start?: number | null;
  timestamp_end?: number | null;
}

export interface RecordingInterpretationUpdate {
  interpretation_session_id: string;
  session_revision: number;
  // Authoritative full list when is_snapshot is true/absent (legacy shape).
  steps: Array<RecordingDraftStep>;
  // Delta upserts (by step_id) when is_snapshot is false. Absent on legacy
  // full-snapshot messages, so a backend that still sends snapshots is a no-op.
  changed_steps?: Array<RecordingDraftStep>;
  is_snapshot?: boolean;
  pending: boolean;
  finalized: boolean;
}

/**
 * Merge delta step upserts into the current list: replace any step sharing a
 * step_id in place (preserving order), and append genuinely-new steps in arrival
 * order. Idempotent — re-applying the same step by id is safe.
 */
export function upsertDraftSteps(
  current: Array<RecordingDraftStep>,
  changed: Array<RecordingDraftStep>,
): Array<RecordingDraftStep> {
  if (changed.length === 0) {
    return current;
  }
  const changedById = new Map(changed.map((step) => [step.step_id, step]));
  const merged = current.map((step) => changedById.get(step.step_id) ?? step);
  const existingIds = new Set(current.map((step) => step.step_id));
  for (const step of changed) {
    if (!existingIds.has(step.step_id)) {
      merged.push(step);
      existingIds.add(step.step_id);
    }
  }
  return merged;
}

export type RecordingDraftStepPatch = Partial<
  Pick<
    RecordingDraftStep,
    "label" | "title" | "navigation_goal" | "url" | "wait_sec"
  >
>;

/**
 * Action kinds the frontend can optimistically predict from a single exfiltrated
 * event, before the backend interprets it. Navigations are deliberately excluded:
 * a link click yields a click action (not a goto) on the backend, so an optimistic
 * nav would have nothing to resolve into.
 */
export type OptimisticActionKind = "click" | "input_text";

/**
 * A frontend-only placeholder appended the instant a significant interaction is
 * observed, so the count and feed advance without waiting for the debounced
 * backend interpretation. Transient: cleared once interpretation settles (the
 * authoritative steps then represent the interaction). Never sent on commit.
 */
export interface OptimisticStep {
  local_id: string;
  action_kind: OptimisticActionKind;
  title: string;
  timestamp: number;
}

let optimisticStepSeq = 0;
export const nextOptimisticStepId = (): string =>
  `optimistic-${(optimisticStepSeq += 1)}`;

/**
 * A frame grabbed from the live VNC canvas at the moment of a recorded click,
 * matched to draft steps by source-event timestamp (ms epoch).
 */
export interface RecordingScreenshot {
  timestampMs: number;
  dataUrl: string;
  xp: number | null;
  yp: number | null;
}

const MAX_SCREENSHOTS = 40;
// dataUrl.length tracks base64-encoded JPEG bytes; cap total in-memory footprint.
const MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024;

/** Tolerance when matching a screenshot to a step's source-event window. */
const SCREENSHOT_MATCH_TOLERANCE_MS = 750;

/**
 * Number of events per compressed chunk.
 */
export const CHUNK_SIZE = 1000 as const;

interface RecordingStore {
  /**
   * Compressed chunks of recorded events (base64 gzip).
   * Each chunk contains up to CHUNK_SIZE events.
   */
  compressedChunks: string[];
  /**
   * The number of events to show the user. This elides noisy events, like
   * `mousemove`.
   */
  exposedEventCount: number;
  /**
   * Buffer of events not yet compressed into a chunk.
   */
  pendingEvents: MessageInExfiltratedEvent[];
  /**
   * Whether a compression operation is currently in progress.
   */
  isCompressing: boolean;
  /**
   * Whether the user is currently in browser recording mode.
   */
  isRecording: boolean;
  recordingStartedAtMs: number | null;
  /**
   * The workflow the recording will be committed to. Required to enable
   * backend live interpretation.
   */
  workflowPermanentId: string | null;
  /**
   * Per-recording id sent on begin-exfiltration. Stable across reconnects of
   * the same recording, regenerated for each new recording, so the backend can
   * tell a reconnect (reuse session) from a new recording (fresh session).
   */
  recordingAttemptId: string | null;
  /**
   * Latest live-interpretation snapshot from the backend. Snapshots are
   * full replacements keyed by monotonically increasing session revision.
   */
  draftSteps: Array<RecordingDraftStep>;
  interpretationSessionId: string | null;
  sessionRevision: number;
  /**
   * Frontend-only optimistic placeholders shown instantly on each significant
   * interaction, cleared once interpretation settles.
   */
  optimisticSteps: Array<OptimisticStep>;
  /**
   * True between "significant events arrived" and "interpretation resolved" —
   * drives the trailing "Interpreting…" placeholder in the feed.
   */
  interpretationPending: boolean;
  /**
   * True once the backend has flushed the session (after end-exfiltration).
   */
  interpretationFinalized: boolean;
  /**
   * Local user overlays. Snapshots are authoritative for step content; user
   * deletes/edits are re-applied on top of every snapshot.
   */
  deletedStepIds: Array<string>;
  stepPatches: Record<string, RecordingDraftStepPatch>;
  /**
   * Nested count of in-progress live draft title edits. While > 0, capture is
   * paused alongside manualCapturePaused.
   */
  draftEditDepth: number;
  /**
   * Operator toggled pause — stops exfiltration and live interpretation until
   * resumed.
   */
  manualCapturePaused: boolean;
  screenshots: Array<RecordingScreenshot>;
  /**
   * Set when the user hits Done: exfiltration stops, and the commit fires once
   * the finalized interpretation snapshot arrives (or a timeout elapses).
   */
  finishRequested: boolean;
  /**
   * True while the process_recording mutation is in flight.
   */
  isCommitting: boolean;
  applyInterpretationUpdate: (update: RecordingInterpretationUpdate) => void;
  /**
   * Append an optimistic placeholder. No-op unless actively recording and
   * capture is not paused.
   */
  addOptimisticStep: (step: OptimisticStep) => void;
  deleteDraftStep: (stepId: string) => void;
  patchDraftStep: (stepId: string, patch: RecordingDraftStepPatch) => void;
  beginDraftEdit: () => void;
  endDraftEdit: () => void;
  setManualCapturePaused: (paused: boolean) => void;
  isCapturePaused: () => boolean;
  addScreenshot: (screenshot: RecordingScreenshot) => void;
  requestFinish: () => void;
  setIsCommitting: (isCommitting: boolean) => void;
  /**
   * Draft steps to commit: snapshot minus user deletions, with user edits
   * applied. Null when live interpretation never produced a revision (caller
   * should fall back to raw event processing).
   */
  getFinalDraftSteps: () => Array<RecordingDraftStep> | null;
  /**
   * Add a new recorded event. Triggers async compression when buffer is full.
   */
  add: (event: MessageInExfiltratedEvent) => void;
  /**
   * Clear all recorded events and compressed chunks.
   */
  clear: () => void;
  /**
   * Reset the recording store (clear events and set isRecording to false).
   */
  reset: () => void;
  /**
   * Set whether the user is in browser recording mode.
   */
  setIsRecording: (
    isRecording: boolean,
    meta?: {
      workflowPermanentId?: string | null;
      browserSessionId?: string | null;
    },
  ) => void;
  /**
   * Flush any pending events into a compressed chunk.
   * Call this before consuming the data.
   */
  flush: () => Promise<void>;
  /**
   * Get all compressed chunks (after flushing pending events).
   */
  getCompressedChunks: () => Promise<string[]>;
  /**
   * Get the total number of events (compressed + pending).
   */
  getEventCount: () => number;
  getSecondsRecording: () => number;
}

/**
 * compresses a JSON string using the Gzip algorithm and returns the result
 * as a Base64 encoded string
 */
async function compressEventsToB64(jsonString: string): Promise<string> {
  // 1. Convert the string to a Uint8Array (a byte array).
  const encoder = new TextEncoder();
  const uint8Array = encoder.encode(jsonString);

  // 2. Create a ReadableStream from the byte array.
  const readableStream = new ReadableStream({
    start(controller) {
      controller.enqueue(uint8Array);
      controller.close();
    },
  });

  // 3. Pipe the data through the Gzip compression stream.
  const compressedStream = readableStream.pipeThrough(
    new CompressionStream("gzip"), // Use 'gzip' for standard network transport
  );

  // 4. Read the entire compressed stream back into a single ArrayBuffer.
  // The Response object provides an easy way to convert streams into a single buffer.
  const compressedBuffer = await new Response(compressedStream).arrayBuffer();

  // 5. Convert the ArrayBuffer (binary data) to a Base64 string for transport.
  // Base64 is used to safely transmit binary data over text-based protocols (like JSON).
  const bytes = new Uint8Array(compressedBuffer);
  let binary = "";

  // Convert Uint8Array to a raw binary string (this is needed for btoa)
  for (let i = 0; i < bytes.length; i++) {
    const nextByte = bytes[i];

    if (nextByte === undefined) {
      continue;
    }

    binary += String.fromCharCode(nextByte);
  }

  // Convert the raw binary string to Base64
  return btoa(binary);
}

export function applyDraftStepOverlays(
  steps: Array<RecordingDraftStep>,
  deletedStepIds: Array<string>,
  stepPatches: Record<string, RecordingDraftStepPatch>,
): Array<RecordingDraftStep> {
  const deleted = new Set(deletedStepIds);
  return steps
    .filter((step) => !deleted.has(step.step_id))
    .map((step) => {
      const patch = stepPatches[step.step_id];
      return patch ? { ...step, ...patch } : step;
    });
}

export function countVisibleDraftSteps(
  steps: Array<RecordingDraftStep>,
  deletedStepIds: Array<string>,
): number {
  if (deletedStepIds.length === 0) {
    return steps.length;
  }

  const deleted = new Set(deletedStepIds);
  let count = 0;
  for (const step of steps) {
    if (!deleted.has(step.step_id)) {
      count += 1;
    }
  }
  return count;
}

/**
 * The screenshot taken closest to the step's source-event window, if any.
 *
 * Both sides of the comparison are the same clock: the screenshot's
 * `timestampMs` is the exfiltrated event's `params.timestamp` (remote-browser
 * `Date.now()`, ms epoch), and the step's `timestamp_start`/`timestamp_end`
 * are the backend echoing that same source-event timestamp back
 * (RecordingDraftStep "Source-action event timestamps (ms epoch)").
 */
export function findScreenshotForStep(
  step: RecordingDraftStep,
  screenshots: Array<RecordingScreenshot>,
): RecordingScreenshot | null {
  const start = step.timestamp_start;
  if (start === null || start === undefined) {
    return null;
  }
  const end = step.timestamp_end ?? start;

  let best: RecordingScreenshot | null = null;
  let bestDistance = Infinity;
  for (const screenshot of screenshots) {
    const t = screenshot.timestampMs;
    const distance = t < start ? start - t : t > end ? t - end : 0; // 0 inside the window
    if (distance <= SCREENSHOT_MATCH_TOLERANCE_MS && distance < bestDistance) {
      best = screenshot;
      bestDistance = distance;
    }
  }
  return best;
}

const EXPOSED_CONSOLE_EVENT_TYPES = new Set(["focus", "click", "keypress"]);

const isExposedEvent = (event: MessageInExfiltratedEvent): boolean => {
  if (event.source === "console") {
    if (EXPOSED_CONSOLE_EVENT_TYPES.has(event.params.type)) {
      return true;
    }
  }

  if (event.source === "cdp") {
    // net:* events are page-activity telemetry, not user interactions
    return !event.event_name.startsWith("net:");
  }

  return false;
};

/**
 * Fields cleared by both `clear()` and `reset()`. Returns fresh collections on
 * each call so no two resets share array/object references. `reset()` layers on
 * the extra teardown (`isCompressing`, `isRecording`, `workflowPermanentId`).
 */
function emptyRecordingState() {
  return {
    compressedChunks: [] as string[],
    exposedEventCount: 0,
    pendingEvents: [] as MessageInExfiltratedEvent[],
    recordingStartedAtMs: null as number | null,
    draftSteps: [] as Array<RecordingDraftStep>,
    interpretationSessionId: null as string | null,
    sessionRevision: 0,
    optimisticSteps: [] as Array<OptimisticStep>,
    interpretationPending: false,
    interpretationFinalized: false,
    deletedStepIds: [] as Array<string>,
    stepPatches: {} as Record<string, RecordingDraftStepPatch>,
    draftEditDepth: 0,
    manualCapturePaused: false,
    screenshots: [] as Array<RecordingScreenshot>,
    finishRequested: false,
    isCommitting: false,
  };
}

export const useRecordingStore = create<RecordingStore>((set, get) => ({
  compressedChunks: [],
  exposedEventCount: 0,
  pendingEvents: [],
  isCompressing: false,
  isRecording: false,
  recordingStartedAtMs: null,
  workflowPermanentId: null,
  recordingAttemptId: null,
  draftSteps: [],
  interpretationSessionId: null,
  sessionRevision: 0,
  optimisticSteps: [],
  interpretationPending: false,
  interpretationFinalized: false,
  deletedStepIds: [],
  stepPatches: {},
  draftEditDepth: 0,
  manualCapturePaused: false,
  screenshots: [],
  finishRequested: false,
  isCommitting: false,

  applyInterpretationUpdate: (update) => {
    const state = get();
    if (!state.isRecording && !state.finishRequested) {
      return;
    }

    // Drop non-finalized snapshots while capture is paused so in-progress edits
    // are not overwritten; finalized snapshots still commit after Done.
    if (
      state.isCapturePaused() &&
      !update.finalized &&
      !state.finishRequested
    ) {
      return;
    }

    // The initial null -> first-id transition is not a session change: it must
    // not discard optimistic steps captured before the first snapshot.
    const sessionChanged =
      state.interpretationSessionId !== null &&
      update.interpretation_session_id !== state.interpretationSessionId;

    if (!sessionChanged && update.session_revision <= state.sessionRevision) {
      return;
    }

    // Optimistic placeholders are transient. Keep them only while interpretation
    // is in flight (pending); clear them once it settles, on session change, or
    // when finalized — by then the authoritative steps represent the interaction.
    // The backend emits an immediate pending=true update (steps unchanged) on each
    // significant event, so this preserves the placeholders we just added and only
    // clears them when the debounced interpret pass lands (pending=false). This
    // avoids permanent residue from events the backend folds away (link-click
    // navigations, deduped/collapsed actions) or duplicate transport events.
    const settled = !update.pending;
    const clearOptimistic = sessionChanged || update.finalized || settled;

    // Delta updates upsert changed steps into the current list; snapshots (the
    // legacy shape, is_snapshot true/absent) replace it wholesale. This keeps the
    // reader working unchanged until the backend starts sending deltas.
    const nextDraftSteps =
      update.is_snapshot === false
        ? upsertDraftSteps(state.draftSteps, update.changed_steps ?? [])
        : update.steps;

    set({
      draftSteps: nextDraftSteps,
      interpretationSessionId: update.interpretation_session_id,
      sessionRevision: update.session_revision,
      ...(clearOptimistic ? { optimisticSteps: [] } : {}),
      interpretationPending: update.pending,
      interpretationFinalized: update.finalized,
      ...(sessionChanged ? { deletedStepIds: [], stepPatches: {} } : {}),
    });
  },

  addOptimisticStep: (step) => {
    const state = get();
    if (!state.isRecording || state.finishRequested) {
      return;
    }
    if (state.isCapturePaused()) {
      return;
    }
    set({ optimisticSteps: [...state.optimisticSteps, step] });
  },

  deleteDraftStep: (stepId) => {
    const state = get();
    if (state.deletedStepIds.includes(stepId)) {
      return;
    }
    set({ deletedStepIds: [...state.deletedStepIds, stepId] });
    captureRecordBrowser("record_browser.draft_step_deleted", {
      step_id: stepId,
    });
  },

  patchDraftStep: (stepId, patch) => {
    const state = get();
    set({
      stepPatches: {
        ...state.stepPatches,
        [stepId]: { ...state.stepPatches[stepId], ...patch },
      },
    });
    captureRecordBrowser("record_browser.draft_step_edited", {
      step_id: stepId,
      fields: Object.keys(patch),
    });
  },

  beginDraftEdit: () => {
    set({ draftEditDepth: get().draftEditDepth + 1 });
  },

  endDraftEdit: () => {
    set({ draftEditDepth: Math.max(0, get().draftEditDepth - 1) });
  },

  setManualCapturePaused: (paused) => {
    set({ manualCapturePaused: paused });
    captureRecordBrowser(
      paused
        ? "record_browser.capture_paused"
        : "record_browser.capture_resumed",
    );
  },

  isCapturePaused: () => {
    const state = get();
    return state.manualCapturePaused || state.draftEditDepth > 0;
  },

  addScreenshot: (screenshot) => {
    const screenshots = [...get().screenshots, screenshot];
    let totalBytes = screenshots.reduce(
      (sum, item) => sum + item.dataUrl.length,
      0,
    );
    while (screenshots.length > 1 && totalBytes > MAX_SCREENSHOT_BYTES) {
      const removed = screenshots.shift();
      if (removed) {
        totalBytes -= removed.dataUrl.length;
      }
    }
    if (screenshots.length > MAX_SCREENSHOTS) {
      screenshots.splice(0, screenshots.length - MAX_SCREENSHOTS);
    }
    set({ screenshots });
  },

  requestFinish: () => {
    if (get().finishRequested) {
      return;
    }
    set({ finishRequested: true });
  },

  setIsCommitting: (isCommitting) => set({ isCommitting }),

  getFinalDraftSteps: () => {
    const state = get();
    if (state.sessionRevision === 0) {
      return null;
    }
    return applyDraftStepOverlays(
      state.draftSteps,
      state.deletedStepIds,
      state.stepPatches,
    );
  },

  add: (event) => {
    const state = get();
    const newPendingEvents = [...state.pendingEvents, event];

    if (isExposedEvent(event)) {
      set({ exposedEventCount: state.exposedEventCount + 1 });
    }

    if (newPendingEvents.length >= CHUNK_SIZE && !state.isCompressing) {
      if (Math.random() < EVENT_CAPTURED_SAMPLE_RATE) {
        captureRecordBrowser("record_browser.event_captured", {
          event_count: state.compressedChunks.length * CHUNK_SIZE + CHUNK_SIZE,
          source: event.source,
        });
      }

      const eventsToCompress = newPendingEvents.slice(0, CHUNK_SIZE);
      const remainingEvents = newPendingEvents.slice(CHUNK_SIZE);

      set({ pendingEvents: remainingEvents, isCompressing: true });

      // compress asynchronously
      queueMicrotask(async () => {
        try {
          const jsonString = JSON.stringify(eventsToCompress);
          const compressed = await compressEventsToB64(jsonString);
          const currentState = get();
          set({
            compressedChunks: [...currentState.compressedChunks, compressed],
            isCompressing: false,
          });
        } catch (error) {
          console.error("Failed to compress events chunk:", error);

          // on error, put events back into pending
          const currentState = get();
          set({
            pendingEvents: [...eventsToCompress, ...currentState.pendingEvents],
            isCompressing: false,
          });
        }
      });
    } else {
      set({ pendingEvents: newPendingEvents });
    }
  },

  clear: () => {
    // Reset per-recording so ids restart at 1 each recording instead of drifting
    // upward across the tab's lifetime.
    optimisticStepSeq = 0;
    set(emptyRecordingState());
  },

  reset: () =>
    set({
      ...emptyRecordingState(),
      isCompressing: false,
      isRecording: false,
      workflowPermanentId: null,
    }),

  setIsRecording: (isRecording, meta) => {
    if (isRecording) {
      const state = get();
      if (!state.isRecording) {
        get().clear();
        set({
          isRecording: true,
          recordingStartedAtMs: Date.now(),
          workflowPermanentId: meta?.workflowPermanentId ?? null,
          recordingAttemptId: crypto.randomUUID(),
        });
        captureRecordBrowser("record_browser.started", {
          workflow_permanent_id: meta?.workflowPermanentId ?? undefined,
          browser_session_id: meta?.browserSessionId ?? undefined,
        });
      }
      return;
    }

    set({
      isRecording: false,
      recordingStartedAtMs: null,
      finishRequested: false,
      isCommitting: false,
    });
  },

  flush: async () => {
    // Wait for any in-progress compression to complete
    while (get().isCompressing) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }

    const pending = get().pendingEvents;
    if (pending.length === 0) {
      return;
    }

    set({ isCompressing: true });

    try {
      const jsonString = JSON.stringify(pending);
      const compressed = await compressEventsToB64(jsonString);
      const currentState = get();
      set({
        compressedChunks: [...currentState.compressedChunks, compressed],
        pendingEvents: [],
        isCompressing: false,
      });
    } catch (error) {
      console.error("Failed to flush pending events:", error);
      set({ isCompressing: false });
      throw error;
    }
  },

  getCompressedChunks: async () => {
    await get().flush();
    return get().compressedChunks;
  },

  getEventCount: () => {
    const state = get();
    return (
      state.compressedChunks.length * CHUNK_SIZE + state.pendingEvents.length
    );
  },

  getSecondsRecording: () => {
    const started = get().recordingStartedAtMs;
    if (started === null) {
      return 0;
    }
    return (Date.now() - started) / 1000;
  },
}));
