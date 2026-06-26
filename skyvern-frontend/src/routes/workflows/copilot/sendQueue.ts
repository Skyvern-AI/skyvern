import { shouldQueuePromptForLiveBrowser } from "./browserReadiness";

// A queued prompt waits for one of two reasons: the live browser isn't ready
// yet ("live_browser"), or Copilot is mid-turn and the message is for the next
// turn ("working"). The two drain on different signals.
export type QueuedPromptReason = "live_browser" | "working";

export type SendAction =
  | "send"
  | "queue_working"
  | "queue_live_browser"
  | "noop";

type ResolveSendInput = {
  // True while a turn is streaming. MUST be read from a synchronous ref, not
  // React state: a rapid double-submit runs a stale closure where the state is
  // still false, which would start a second concurrent stream.
  inFlight: boolean;
  hasQueuedPrompt: boolean;
  requiresLiveBrowser: boolean;
  isLiveBrowserReady: boolean;
  candidate: string;
  // True when this call is draining an already-queued prompt (it carries the
  // queued message id), so the "one queued prompt at a time" guard is bypassed.
  isDrain: boolean;
  skipQueue: boolean;
};

export function resolveSendAction({
  inFlight,
  hasQueuedPrompt,
  requiresLiveBrowser,
  isLiveBrowserReady,
  candidate,
  isDrain,
  skipQueue,
}: ResolveSendInput): SendAction {
  if (!candidate.trim()) {
    return "noop";
  }
  if (hasQueuedPrompt && !isDrain) {
    return "noop";
  }
  if (inFlight) {
    return "queue_working";
  }
  if (
    !skipQueue &&
    shouldQueuePromptForLiveBrowser({
      requiresLiveBrowser,
      isLiveBrowserReady,
      message: candidate,
    })
  ) {
    return "queue_live_browser";
  }
  return "send";
}

export type DrainAction = "drain_skip_queue" | "drain_requeue" | "wait";

type ResolveDrainInput = {
  queuedReason: QueuedPromptReason | null;
  inFlight: boolean;
  hasLiveBrowserSession: boolean;
  hasWorkflowPermanentId: boolean;
};

// drain_skip_queue is the ONLY path that sends past the live-browser predicate,
// and only for a live_browser-reason prompt whose session is confirmed — so a
// prompt is never sent with a null browser session. A working-reason prompt
// always re-enters handleSend without skipQueue (drain_requeue) so the
// live-browser predicate re-gates it.
export function resolveDrainAction({
  queuedReason,
  inFlight,
  hasLiveBrowserSession,
  hasWorkflowPermanentId,
}: ResolveDrainInput): DrainAction {
  if (queuedReason === null || inFlight || !hasWorkflowPermanentId) {
    return "wait";
  }
  if (queuedReason === "working") {
    return "drain_requeue";
  }
  if (queuedReason === "live_browser" && hasLiveBrowserSession) {
    return "drain_skip_queue";
  }
  return "wait";
}
