import { useEffect, useRef, useState } from "react";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getRuntimeApiKey, newWssBaseUrl } from "@/util/env";

export type VerificationRequest = {
  type: "verification_code";
  task_id?: string;
  workflow_run_id?: string;
  identifier?: string | null;
  polling_started_at?: string | null;
};

// Aliased for future-proofing if more event types are added
export type NotificationEvent = VerificationRequest;

type NotificationMessage = Omit<NotificationEvent, "type"> & {
  type: string;
};

const getRequestKey = (msg: Partial<NotificationMessage>) =>
  msg.task_id ?? msg.workflow_run_id ?? "";

export function useNotificationStream() {
  // Use a Record (object) instead of a Map for easier immutable state updates
  const [events, setEvents] = useState<Record<string, NotificationEvent>>({});

  const credentialGetter = useCredentialGetter();
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectDelayRef = useRef<number>(3000);
  const authFailedRef = useRef<boolean>(false);

  useEffect(() => {
    let isCancelled = false;

    const connect = async () => {
      // Prevent connecting if tab is hidden, auth failed, or unmounted
      if (isCancelled || authFailedRef.current || document.hidden) return;

      // Flatten authorization query building
      let authQuery = "";
      if (credentialGetter) {
        authQuery = `?token=Bearer ${await credentialGetter()}`;
      } else if (getRuntimeApiKey()) {
        authQuery = `?apikey=${getRuntimeApiKey()}`;
      }

      if (!authQuery || isCancelled) return;

      // Clean up existing socket before opening a new one
      socketRef.current?.close();

      const socket = new WebSocket(
        `${newWssBaseUrl}/stream/notifications${authQuery}`,
      );
      socketRef.current = socket;
      let connectionOpened = false;

      // Use direct event handlers (onopen) instead of addEventListener for readability
      socket.onopen = () => {
        connectionOpened = true;
        reconnectDelayRef.current = 3000; // Reset backoff on success
      };

      socket.onmessage = ({ data }) => {
        try {
          const msg: NotificationMessage = JSON.parse(data);
          if (["heartbeat", "timeout"].includes(msg.type)) return;

          const key = getRequestKey(msg);
          if (!key) return;

          setEvents((prev) => {
            const next = { ...prev }; // Easier to mutate a shallow copy than a Map

            if (msg.type === "verification_code_required") {
              next[key] = {
                type: "verification_code",
                task_id: msg.task_id,
                workflow_run_id: msg.workflow_run_id,
                identifier: msg.identifier,
                polling_started_at: msg.polling_started_at,
              };
            } else if (msg.type === "verification_code_resolved") {
              delete next[key];
            }
            return next;
          });
        } catch {
          // Ignore malformed JSON
        }
      };

      socket.onclose = (event) => {
        if (socketRef.current !== socket || isCancelled) return;

        const isAuthFailure =
          event.code === 1002 || (!connectionOpened && event.code === 1006);

        if (isAuthFailure) {
          console.warn("WebSocket auth failed. Stopping reconnects.", {
            code: event.code,
          });
          authFailedRef.current = true;
          return;
        }

        // Exponential backoff for normal disconnections
        reconnectTimerRef.current = setTimeout(
          connect,
          reconnectDelayRef.current,
        );
        reconnectDelayRef.current = Math.min(
          reconnectDelayRef.current * 2,
          30000,
        );
      };

      socket.onerror = () => {
        if (socketRef.current === socket) socket.close();
      };
    };

    const handleVisibilityChange = () => {
      if (document.hidden) {
        if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
        socketRef.current?.close();
        socketRef.current = null;
      } else {
        reconnectDelayRef.current = 3000;
        connect();
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    connect();

    return () => {
      isCancelled = true;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      socketRef.current?.close();
    };
  }, [credentialGetter]);

  // Derive final arrays from the object state
  const eventList = Object.values(events);
  const verificationRequests = eventList.filter(
    (e): e is VerificationRequest => e.type === "verification_code",
  );

  return { events: eventList, verificationRequests };
}
