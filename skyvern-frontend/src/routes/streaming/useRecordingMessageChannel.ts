import { useCallback, useEffect, useRef, useState } from "react";

import { toast } from "@/components/ui/use-toast";
import { buildOptimisticStep } from "@/routes/workflows/editor/recording/optimisticSteps";
import {
  useRecordingStore,
  type ExfiltratedEventConsoleParams,
  type MessageInExfiltratedEvent,
  type RecordingDraftStep,
  type RecordingInterpretationUpdate,
} from "@/store/useRecordingStore";
import { copyText } from "@/util/copyText";
import { newWssBaseUrl } from "@/util/env";

import { useWebSocketParams } from "./webSocketParams";

export type RecordingClipboardMode = "vnc" | "message" | "none";

export interface UseRecordingMessageChannelOptions {
  browserSessionId: string | null;
  /** socket lifecycle gate: open the message WS while true */
  enabled: boolean;
  /** recording active: drives begin/end-exfiltration on edges */
  exfiltrate: boolean;
  workflowPermanentId: string | null;
  /** returns a data URL of the current frame for click screenshots, or null */
  getFrameDataUrl?: () => string | null;
  clipboard: RecordingClipboardMode;
  socketUrl?: string;
  reconnectTrigger?: number;
  onConnectionChange?: (connected: boolean, event?: CloseEvent) => void;
}

interface CommandBeginExfiltration {
  kind: "begin-exfiltration";
  workflow_permanent_id?: string;
  live_interpretation_enabled?: boolean;
  // Declares that this client understands delta interpretation updates, so the
  // backend may send changed_steps instead of full snapshots.
  supports_interpretation_deltas?: boolean;
  // Per-recording id: stable across reconnects, new per recording, so the
  // backend reuses the session on reconnect but starts fresh on a new recording.
  recording_attempt_id?: string;
}

interface CommandCedeControl {
  kind: "cede-control";
}

interface CommandEndExfiltration {
  kind: "end-exfiltration";
}

interface CommandTakeControl {
  kind: "take-control";
}

interface CommandRecordingCapturePause {
  kind: "recording-capture-pause";
}

interface CommandRecordingCaptureResume {
  kind: "recording-capture-resume";
}

interface CommandRecordingRearmCapture {
  kind: "recording-rearm-capture";
}

interface CommandAskForClipboardResponse {
  kind: "ask-for-clipboard-response";
  text: string;
}

interface CommandClipboardCopy {
  kind: "clipboard-copy";
}

interface CommandClipboardPaste {
  kind: "clipboard-paste";
  text: string;
}

// a "Command" is a fire-n-forget out-message - it does not require a response
export type Command =
  | CommandAskForClipboardResponse
  | CommandBeginExfiltration
  | CommandCedeControl
  | CommandClipboardCopy
  | CommandClipboardPaste
  | CommandEndExfiltration
  | CommandRecordingCapturePause
  | CommandRecordingCaptureResume
  | CommandRecordingRearmCapture
  | CommandTakeControl;

export interface UseRecordingMessageChannelResult {
  messageSocket: WebSocket | null;
  isMessageConnected: boolean;
  /** send a command over the message socket if open */
  sendCommand: (command: Command) => void;
}

const messageInKinds = [
  "ask-for-clipboard",
  "copied-text",
  "error",
  "exfiltrated-event",
  "recording-committed",
  "recording-interpretation-update",
] as const;

type MessageInKind = (typeof messageInKinds)[number];

interface MessageInAskForClipboard {
  kind: "ask-for-clipboard";
}

interface MessageInCopiedText {
  kind: "copied-text";
  text: string;
}

interface MessageInError {
  kind: "error";
  failed_kind: string;
  message: string;
}

interface MessageInRecordingInterpretationUpdate extends RecordingInterpretationUpdate {
  kind: "recording-interpretation-update";
}

export interface RecordingCommitResult {
  blocks: Array<unknown>;
  parameters: Array<unknown>;
  mode: string;
  diagnostics: Record<string, number>;
}

interface MessageInRecordingCommitted extends RecordingCommitResult {
  kind: "recording-committed";
}

type MessageIn =
  | MessageInCopiedText
  | MessageInError
  | MessageInAskForClipboard
  | MessageInExfiltratedEvent
  | MessageInRecordingCommitted
  | MessageInRecordingInterpretationUpdate;

function getMessage(data: unknown): MessageIn | undefined {
  if (!data) {
    return;
  }

  if (typeof data !== "object") {
    return;
  }

  if (!("kind" in data)) {
    return;
  }

  const k = data.kind;

  if (typeof k !== "string") {
    return;
  }

  if (!messageInKinds.includes(k as MessageInKind)) {
    return;
  }

  const kind = k as MessageInKind;

  switch (kind) {
    case "ask-for-clipboard": {
      return data as MessageInAskForClipboard;
    }
    case "copied-text": {
      if ("text" in data && typeof data.text === "string") {
        return {
          kind: "copied-text",
          text: data.text,
        };
      }
      break;
    }
    case "error": {
      if (
        "failed_kind" in data &&
        typeof data.failed_kind === "string" &&
        "message" in data &&
        typeof data.message === "string"
      ) {
        return {
          kind: "error",
          failed_kind: data.failed_kind,
          message: data.message,
        };
      }
      break;
    }
    case "exfiltrated-event": {
      if (
        "event_name" in data &&
        typeof data.event_name === "string" &&
        "params" in data &&
        typeof data.params === "object" &&
        data.params !== null &&
        "source" in data &&
        typeof data.source === "string"
      ) {
        const event = data as MessageInExfiltratedEvent;

        return {
          kind: "exfiltrated-event",
          event_name: event.event_name,
          params: event.params,
          source: event.source,
          timestamp: event.timestamp,
        } as MessageInExfiltratedEvent;
      }
      break;
    }
    case "recording-committed": {
      return data as MessageInRecordingCommitted;
    }
    case "recording-interpretation-update": {
      // steps is optional: a delta update carries changed_steps instead. Only
      // session_revision is required to accept the message.
      if (
        "session_revision" in data &&
        typeof data.session_revision === "number"
      ) {
        const update = data as MessageInRecordingInterpretationUpdate;
        return {
          kind: "recording-interpretation-update",
          interpretation_session_id:
            typeof update.interpretation_session_id === "string"
              ? update.interpretation_session_id
              : "",
          session_revision: update.session_revision,
          steps: Array.isArray(update.steps) ? update.steps : [],
          changed_steps: Array.isArray(update.changed_steps)
            ? update.changed_steps
            : [],
          // Absent/true => full snapshot (legacy). Only false triggers delta merge.
          is_snapshot:
            typeof update.is_snapshot === "boolean" ? update.is_snapshot : true,
          pending: typeof update.pending === "boolean" ? update.pending : false,
          finalized:
            typeof update.finalized === "boolean" ? update.finalized : false,
        };
      }
      break;
    }
    default: {
      const _exhaustive: never = kind;
      return _exhaustive;
    }
  }
}

function captureRecordingScreenshot(
  params: ExfiltratedEventConsoleParams,
  getFrameDataUrl: (() => string | null) | undefined,
) {
  const schedule =
    typeof requestIdleCallback === "function"
      ? (fn: () => void) => requestIdleCallback(fn, { timeout: 750 })
      : (fn: () => void) => window.setTimeout(fn, 0);

  schedule(() => {
    try {
      const dataUrl = getFrameDataUrl?.();
      if (!dataUrl) {
        return;
      }
      useRecordingStore.getState().addScreenshot({
        timestampMs: params.timestamp,
        dataUrl,
        xp: params.mousePosition.xp,
        yp: params.mousePosition.yp,
      });
    } catch {
      // toDataURL can throw on a tainted/headless canvas; shots are optional
    }
  });
}

function handleMessage(
  data: unknown,
  ws: WebSocket | null,
  clipboard: RecordingClipboardMode,
  getFrameDataUrl: (() => string | null) | undefined,
  onBeginExfiltrationError: () => void,
) {
  const message = getMessage(data);

  if (!message) {
    console.warn("Unknown message received on message channel:", data);
    return;
  }

  const kind = message.kind;

  const respond = (response: CommandAskForClipboardResponse) => {
    if (!ws) {
      console.warn("Cannot send message, as message socket is null.");
      console.warn(response);
      return;
    }

    ws.send(JSON.stringify(response));
  };

  switch (kind) {
    case "ask-for-clipboard": {
      if (clipboard !== "vnc") {
        break;
      }
      if (!navigator.clipboard) {
        console.warn("Clipboard API not available.");
        return;
      }

      navigator.clipboard
        .readText()
        .then((text) => {
          toast({
            title: "Pasting Into Browser",
            description:
              "Pasting your current clipboard text into the browser.",
          });

          respond({
            kind: "ask-for-clipboard-response",
            text,
          });
        })
        .catch((err) => {
          console.error("Failed to read clipboard contents: ", err);
        });

      break;
    }
    case "copied-text": {
      if (clipboard === "none") {
        break;
      }
      const text = message.text;

      copyText(text)
        .then((success) => {
          if (success) {
            toast({
              title: "Copied to Clipboard",
              description: "The text has been copied to your clipboard.",
            });
          } else {
            toast({
              variant: "destructive",
              title: "Failed to write to Clipboard",
              description: "The text could not be copied to your clipboard.",
            });
          }
        })
        .catch((err) => {
          console.error("Failed to write to clipboard:", err);

          toast({
            variant: "destructive",
            title: "Failed to write to Clipboard",
            description: "The text could not be copied to your clipboard.",
          });
        });

      break;
    }
    case "error": {
      if (message.failed_kind === "begin-exfiltration") {
        onBeginExfiltrationError();
      } else {
        console.warn("Message channel command failed:", message);
      }
      break;
    }
    case "exfiltrated-event": {
      // Read store state fresh: this handler is attached once per socket
      // and would otherwise see a stale isRecording.
      const store = useRecordingStore.getState();
      if (!store.isRecording && !store.finishRequested) {
        break;
      }
      if (store.isCapturePaused()) {
        break;
      }
      if (
        store.isRecording &&
        message.source === "console" &&
        message.params.type === "click"
      ) {
        captureRecordingScreenshot(message.params, getFrameDataUrl);
      }
      if (
        store.isRecording &&
        !store.finishRequested &&
        message.source === "cdp" &&
        (message.event_name === "nav:frame_navigated" ||
          message.event_name === "nav:navigated_within_document")
      ) {
        if (ws) {
          ws.send(JSON.stringify({ kind: "recording-rearm-capture" }));
        }
      }
      if (store.isRecording && !store.finishRequested) {
        const optimistic = buildOptimisticStep(message);
        if (optimistic) {
          store.addOptimisticStep(optimistic);
        }
      }
      store.add(message);
      break;
    }
    case "recording-committed": {
      // commitRecordingOverMessageSocket resolves this on its own listener.
      break;
    }
    case "recording-interpretation-update": {
      useRecordingStore.getState().applyInterpretationUpdate({
        interpretation_session_id: message.interpretation_session_id,
        session_revision: message.session_revision,
        steps: message.steps,
        changed_steps: message.changed_steps,
        is_snapshot: message.is_snapshot,
        pending: message.pending,
        finalized: message.finalized,
      });
      break;
    }
    default: {
      const _exhaustive: never = kind;
      return _exhaustive;
    }
  }
}

const COMMIT_TIMEOUT_MS = 60_000;

// The v2 ledger is process-local to the pod holding this socket, so the commit rides
// the socket rather than an HTTP POST that could land on another pod.
export function commitRecordingOverMessageSocket(args: {
  mode: "blocks" | "auto";
  draftSteps: Array<RecordingDraftStep> | null;
}): Promise<RecordingCommitResult> {
  const socket = useRecordingStore.getState().messageSocket;
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return Promise.reject(
      new Error("The recording connection is not available; try again."),
    );
  }

  return new Promise<RecordingCommitResult>((resolve, reject) => {
    const settle = (outcome: () => void) => {
      socket.removeEventListener("message", onMessage);
      socket.removeEventListener("close", onClose);
      clearTimeout(timer);
      outcome();
    };

    const onClose = () =>
      settle(() =>
        reject(
          new Error("The recording connection closed before it finished."),
        ),
      );

    const onMessage = (event: MessageEvent) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(event.data);
      } catch {
        return;
      }
      const message = getMessage(parsed);
      if (message?.kind === "recording-committed") {
        settle(() => resolve(message));
      } else if (
        message?.kind === "error" &&
        message.failed_kind === "recording-commit"
      ) {
        settle(() => reject(new Error(message.message)));
      }
    };

    const timer = setTimeout(
      () =>
        settle(() => reject(new Error("Timed out processing the recording."))),
      COMMIT_TIMEOUT_MS,
    );

    socket.addEventListener("message", onMessage);
    socket.addEventListener("close", onClose);
    socket.send(
      JSON.stringify({
        kind: "recording-commit",
        mode: args.mode,
        draft_steps: args.draftSteps,
      }),
    );
  });
}

export function useRecordingMessageChannel(
  options: UseRecordingMessageChannelOptions,
): UseRecordingMessageChannelResult {
  const {
    browserSessionId,
    enabled,
    exfiltrate,
    workflowPermanentId,
    socketUrl,
    reconnectTrigger,
  } = options;
  const [messageSocket, setMessageSocket] = useState<WebSocket | null>(null);
  const [isMessageConnected, setIsMessageConnected] = useState(false);
  const recordingAttemptId = useRecordingStore(
    (state) => state.recordingAttemptId,
  );
  const messageSocketRef = useRef<WebSocket | null>(null);
  const getWebSocketParams = useWebSocketParams();
  const optionsRef = useRef(options);
  const exfiltrateRef = useRef(exfiltrate);
  const beganRef = useRef(false);
  const previousExfiltrateRef = useRef(false);
  const beginRetryAttemptsRef = useRef(0);
  const beginRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  optionsRef.current = options;
  exfiltrateRef.current = exfiltrate;

  const clearBeginRetry = useCallback(() => {
    if (beginRetryTimerRef.current) {
      clearTimeout(beginRetryTimerRef.current);
      beginRetryTimerRef.current = null;
    }
  }, []);

  const sendBeginExfiltration = useCallback(() => {
    const socket = messageSocketRef.current;
    if (
      !socket ||
      socket.readyState !== WebSocket.OPEN ||
      !exfiltrateRef.current
    ) {
      return;
    }
    const currentOptions = optionsRef.current;
    socket.send(
      JSON.stringify({
        kind: "begin-exfiltration",
        workflow_permanent_id: currentOptions.workflowPermanentId ?? undefined,
        live_interpretation_enabled: Boolean(
          currentOptions.workflowPermanentId,
        ),
        supports_interpretation_deltas: true,
        recording_attempt_id:
          useRecordingStore.getState().recordingAttemptId ?? undefined,
      }),
    );
    beganRef.current = true;
  }, []);

  const scheduleBeginRetry = useCallback(() => {
    if (
      !exfiltrateRef.current ||
      beginRetryTimerRef.current ||
      beginRetryAttemptsRef.current >= 5
    ) {
      return;
    }
    beginRetryAttemptsRef.current += 1;
    beginRetryTimerRef.current = setTimeout(() => {
      beginRetryTimerRef.current = null;
      sendBeginExfiltration();
    }, 2000);
  }, [sendBeginExfiltration]);

  useEffect(() => {
    setIsMessageConnected(false);
    setMessageSocket(null);
  }, [browserSessionId]);

  useEffect(() => {
    if (!enabled || !browserSessionId) {
      return;
    }

    let ws: WebSocket | null = null;
    let cancelled = false;

    const handleDisconnected = (
      event: CloseEvent,
      disconnectedSocket: WebSocket | null,
    ) => {
      const store = useRecordingStore.getState();
      if (store.messageSocket === disconnectedSocket) {
        store.setMessageSocket(null);
      }
      if (
        disconnectedSocket === null ||
        messageSocketRef.current === disconnectedSocket
      ) {
        messageSocketRef.current = null;
        setIsMessageConnected(false);
        setMessageSocket(null);
      }
      if (cancelled) return;
      optionsRef.current.onConnectionChange?.(false, event);
    };

    const connect = async () => {
      const wsParams = await getWebSocketParams();
      if (cancelled) return;
      const messageUrl = `${socketUrl ?? `${newWssBaseUrl}/stream/messages/browser_session/${browserSessionId}`}?${wsParams}`;

      ws = new WebSocket(messageUrl);

      ws.onopen = () => {
        if (cancelled) return;
        useRecordingStore.getState().setMessageSocket(ws);
        messageSocketRef.current = ws;
        setIsMessageConnected(true);
        setMessageSocket(ws);
        optionsRef.current.onConnectionChange?.(true);
      };

      ws.onmessage = (event) => {
        const data = event.data;

        try {
          const message = JSON.parse(data);
          const currentOptions = optionsRef.current;
          handleMessage(
            message,
            ws,
            currentOptions.clipboard,
            currentOptions.getFrameDataUrl,
            scheduleBeginRetry,
          );
        } catch (e) {
          console.error(
            "Error parsing message from message channel:",
            e,
            event,
          );
        }
      };

      ws.onclose = (event) => {
        handleDisconnected(event, ws);
      };
    };

    void connect().catch((error) => {
      if (cancelled) return;
      console.error("Failed to set up recording message channel:", error);
      handleDisconnected(
        new CloseEvent("close", { code: 1011, reason: "setup_failed" }),
        null,
      );
    });

    return () => {
      cancelled = true;
      clearBeginRetry();
      // A mid-recording disconnect must leave the backend interpretation
      // session resumable (SKY-12429), so it does not send END. Once the store
      // says recording ended, cleanup flushes END for a same-commit panel
      // unmount that prevented the exfiltrate falling-edge effect from running.
      if (
        ws?.readyState === WebSocket.OPEN &&
        beganRef.current &&
        !useRecordingStore.getState().isRecording
      ) {
        ws.send(JSON.stringify({ kind: "end-exfiltration" }));
        beganRef.current = false;
      }
      if (useRecordingStore.getState().messageSocket === ws) {
        useRecordingStore.getState().setMessageSocket(null);
      }
      messageSocketRef.current = null;
      try {
        ws && ws.close();
      } catch (e) {
        // pass
      }
    };
    // NOTE: adding getWebSocketParams causes constant reconnects of message channel when,
    // for instance, take-control or cede-control is clicked
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    browserSessionId,
    clearBeginRetry,
    enabled,
    reconnectTrigger,
    scheduleBeginRetry,
    socketUrl,
  ]);

  const sendCommand = useCallback((command: Command) => {
    if (!messageSocketRef.current) {
      return;
    }

    messageSocketRef.current.send(JSON.stringify(command));
  }, []);

  // effect for exfiltration
  useEffect(() => {
    const risingEdge = exfiltrate && !previousExfiltrateRef.current;
    previousExfiltrateRef.current = exfiltrate;

    if (exfiltrate) {
      if (risingEdge) {
        beginRetryAttemptsRef.current = 0;
      }
      clearBeginRetry();
      sendBeginExfiltration();
    } else {
      clearBeginRetry();
      beginRetryAttemptsRef.current = 0;
      const socket = messageSocketRef.current;
      if (socket?.readyState === WebSocket.OPEN && beganRef.current) {
        socket.send(JSON.stringify({ kind: "end-exfiltration" }));
        beganRef.current = false;
      }
    }
  }, [
    clearBeginRetry,
    exfiltrate,
    messageSocket,
    recordingAttemptId,
    sendBeginExfiltration,
    workflowPermanentId,
  ]);

  const manualCapturePaused = useRecordingStore(
    (state) => state.manualCapturePaused,
  );
  const draftEditDepth = useRecordingStore((state) => state.draftEditDepth);
  const capturePaused = manualCapturePaused || draftEditDepth > 0;
  const previousCapturePausedRef = useRef(false);

  // Pause exfiltration + live interpretation while the operator edits drafts
  // or explicitly pauses capture.
  useEffect(() => {
    if (!exfiltrate || !messageSocket) {
      // Backend pause state is per exfiltration session, so start the next
      // session's edge detection from "not paused". A recording that ended
      // while paused would otherwise fire a spurious resume on the next start;
      // and if capture IS paused on a mid-recording reconnect, this re-sends
      // the pause to the new socket (idempotent) instead of assuming it.
      previousCapturePausedRef.current = false;
      return;
    }

    const wasPaused = previousCapturePausedRef.current;
    if (!wasPaused && capturePaused) {
      messageSocket.send(JSON.stringify({ kind: "recording-capture-pause" }));
    } else if (wasPaused && !capturePaused) {
      messageSocket.send(JSON.stringify({ kind: "recording-capture-resume" }));
    }

    previousCapturePausedRef.current = capturePaused;
  }, [capturePaused, exfiltrate, messageSocket]);

  return { messageSocket, isMessageConnected, sendCommand };
}
