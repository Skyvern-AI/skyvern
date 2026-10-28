import { useCallback, useEffect, useRef, useState } from "react";

import { toast } from "@/components/ui/use-toast";
import { copyText } from "@/util/copyText";
import { newWssBaseUrl } from "@/util/env";

import { useWebSocketParams } from "./webSocketParams";

export type RecordingClipboardMode = "vnc" | "message" | "none";

export interface UseRecordingMessageChannelOptions {
  browserSessionId: string | null;
  /** socket lifecycle gate: open the message WS while true */
  enabled: boolean;
  clipboard: RecordingClipboardMode;
  socketUrl?: string;
  reconnectTrigger?: number;
  onConnectionChange?: (connected: boolean, event?: CloseEvent) => void;
}

interface CommandCedeControl {
  kind: "cede-control";
}

interface CommandTakeControl {
  kind: "take-control";
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
  | CommandCedeControl
  | CommandClipboardCopy
  | CommandClipboardPaste
  | CommandTakeControl;

export interface UseRecordingMessageChannelResult {
  messageSocket: WebSocket | null;
  isMessageConnected: boolean;
  /** send a command over the message socket if open; false when it was not sent */
  sendCommand: (command: Command) => boolean;
}

const messageInKinds = ["ask-for-clipboard", "copied-text", "error"] as const;

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

type MessageIn =
  | MessageInCopiedText
  | MessageInError
  | MessageInAskForClipboard;

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
    default: {
      const _exhaustive: never = kind;
      return _exhaustive;
    }
  }
}

const CLIPBOARD_FAILURE_TITLES: Record<string, string> = {
  "clipboard-paste": "Paste failed",
  "clipboard-copy": "Copy failed",
};

function handleMessage(
  data: unknown,
  ws: WebSocket | null,
  clipboard: RecordingClipboardMode,
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
      const text = message.text;
      // Copying with nothing selected must leave the local clipboard alone, as a
      // native Cmd/Ctrl+C does.
      if (clipboard === "none" || !text) {
        break;
      }

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
      if (CLIPBOARD_FAILURE_TITLES[message.failed_kind]) {
        toast({
          variant: "destructive",
          title: CLIPBOARD_FAILURE_TITLES[message.failed_kind],
          description: message.message,
        });
      } else {
        console.warn("Message channel command failed:", message);
      }
      break;
    }
    default: {
      const _exhaustive: never = kind;
      return _exhaustive;
    }
  }
}

export function useRecordingMessageChannel(
  options: UseRecordingMessageChannelOptions,
): UseRecordingMessageChannelResult {
  const { browserSessionId, enabled, socketUrl, reconnectTrigger } = options;
  const [messageSocket, setMessageSocket] = useState<WebSocket | null>(null);
  const [isMessageConnected, setIsMessageConnected] = useState(false);
  const messageSocketRef = useRef<WebSocket | null>(null);
  const getWebSocketParams = useWebSocketParams();
  const optionsRef = useRef(options);
  optionsRef.current = options;

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
      const { params, protocols } = await getWebSocketParams();
      if (cancelled) return;
      const messageUrl = `${socketUrl ?? `${newWssBaseUrl}/stream/messages/browser_session/${browserSessionId}`}?${params}`;

      ws = new WebSocket(messageUrl, protocols);

      ws.onopen = () => {
        if (cancelled) return;
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
          handleMessage(message, ws, currentOptions.clipboard);
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
  }, [browserSessionId, enabled, reconnectTrigger, socketUrl]);

  const sendCommand = useCallback((command: Command) => {
    const socket = messageSocketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }

    socket.send(JSON.stringify(command));
    return true;
  }, []);

  return { messageSocket, isMessageConnected, sendCommand };
}
