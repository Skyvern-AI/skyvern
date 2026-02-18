import { useEffect, useRef, useState } from "react";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getRuntimeApiKey, wssBaseUrl } from "@/util/env";

type VerificationRequest = {
  type: "verification_code";
  task_id?: string;
  workflow_run_id?: string;
  identifier?: string | null;
  polling_started_at?: string | null;
};

type NotificationEvent = VerificationRequest;

type NotificationMessage = {
  type: string;
  task_id?: string;
  workflow_run_id?: string;
  identifier?: string | null;
  polling_started_at?: string | null;
};

const requestKey = (msg: { task_id?: string; workflow_run_id?: string }) =>
  msg.task_id ?? msg.workflow_run_id ?? "";

function useNotificationStream() {
  const [eventMap, setEventMap] = useState(
    new Map<string, NotificationEvent>(),
  );
  const credentialGetter = useCredentialGetter();
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;

    const connect = async () => {
      if (cancelled) return;

      const credential = credentialGetter
        ? `?token=Bearer ${await credentialGetter()}`
        : getRuntimeApiKey()
          ? `?apikey=${getRuntimeApiKey()}`
          : "";

      if (!credential || cancelled) return;

      socketRef.current?.close();
      socketRef.current = null;

      const socket = new WebSocket(
        `${wssBaseUrl}/stream/notifications${credential}`,
      );
      socketRef.current = socket;

      socket.addEventListener("message", ({ data }) => {
        try {
          const msg: NotificationMessage = JSON.parse(data);
          if (msg.type === "heartbeat" || msg.type === "timeout") return;
          const key = requestKey(msg);
          if (!key) return;

          setEventMap((prev) => {
            const next = new Map(prev);
            if (msg.type === "verification_code_required") {
              next.set(key, {
                type: "verification_code",
                task_id: msg.task_id,
                workflow_run_id: msg.workflow_run_id,
                identifier: msg.identifier,
                polling_started_at: msg.polling_started_at,
              });
            } else if (msg.type === "verification_code_resolved") {
              next.delete(key);
            }
            return next;
          });
        } catch {
          // Ignore malformed messages
        }
      });

      socket.addEventListener("close", () => {
        if (socketRef.current === socket && !cancelled && !document.hidden) {
          reconnectTimerRef.current = setTimeout(connect, 3000);
        }
      });

      socket.addEventListener("error", () => {
        if (socketRef.current === socket) socket.close();
      });
    };

    const handleVisibilityChange = () => {
      if (document.hidden) {
        reconnectTimerRef.current && clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
        socketRef.current?.close();
        socketRef.current = null;
      } else if (!socketRef.current && !cancelled) {
        connect();
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    connect();

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      reconnectTimerRef.current && clearTimeout(reconnectTimerRef.current);
      socketRef.current?.close();
    };
  }, [credentialGetter]);

  const events = Array.from(eventMap.values());
  const verificationRequests = events.filter(
    (e): e is VerificationRequest => e.type === "verification_code",
  );

  return { events, verificationRequests };
}

export { useNotificationStream };
export type { VerificationRequest, NotificationEvent };
