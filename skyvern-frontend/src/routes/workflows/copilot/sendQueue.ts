import { shouldQueuePromptForLiveBrowser } from "./browserReadiness";

// A queued prompt waits for one of two reasons: the live browser isn't ready
// yet ("live_browser"), or Copilot is mid-turn and the message is for the next
// turn ("working"). The two drain on different signals.
export type QueuedPromptReason = "live_browser" | "working";

export type SendAction =
  | "send"
  | "queue_working"
  | "queue_live_browser"
  | "replace_queued"
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
  // One queued prompt at a time still holds — a second send rewrites the one
  // that's parked rather than being swallowed, so the composer is never a
  // dead box while a turn is parked waiting on the user.
  if (hasQueuedPrompt && !isDrain) {
    return "replace_queued";
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

export type DrainAction =
  | "drain_skip_queue"
  | "drain_requeue"
  | "drop_duplicate"
  | "wait";

type ResolveDrainInput = {
  queuedReason: QueuedPromptReason | null;
  inFlight: boolean;
  hasLiveBrowserSession: boolean;
  hasWorkflowPermanentId: boolean;
  queuedContent: string | null;
  // The message that opened the turn that just ended. Every boolean must be
  // affirmatively true to drop, so a missed terminal path drains as today.
  turnOpeningContent: string | null;
  turnCompletedNormally: boolean;
  turnWorkflowMatches: boolean;
  // The chat-post carries more than the text (mode, fix origin, audio, block
  // target, browser session, code block); a queued send that differs on any of
  // those is not a repeat. workflow_yaml, workflow_run_id and
  // keep_pending_proposal are deliberately excluded: they track turn state the
  // queued message was typed before, not what the user asked for.
  turnRequestMatches: boolean;
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
  queuedContent,
  turnOpeningContent,
  turnCompletedNormally,
  turnWorkflowMatches,
  turnRequestMatches,
}: ResolveDrainInput): DrainAction {
  if (queuedReason === null || inFlight || !hasWorkflowPermanentId) {
    return "wait";
  }
  if (queuedReason === "working") {
    if (
      turnCompletedNormally &&
      turnWorkflowMatches &&
      turnRequestMatches &&
      queuedContent !== null &&
      turnOpeningContent !== null &&
      queuedContent.trim() === turnOpeningContent.trim()
    ) {
      return "drop_duplicate";
    }
    return "drain_requeue";
  }
  if (queuedReason === "live_browser" && hasLiveBrowserSession) {
    return "drain_skip_queue";
  }
  return "wait";
}
