import { useState, useEffect, useRef, createElement } from "react";
import { toast } from "@/components/ui/use-toast";
import { VerificationToastContent } from "@/components/VerificationToast";
import { enable2faNotifications } from "@/util/env";

/** How long (minutes) before we consider the 2FA request timed out */
const VERIFICATION_TIMEOUT_MINS = 15;
/** Seconds remaining at which the countdown turns "critical" (red) */
const CRITICAL_TIME_THRESHOLD_SEC = 60;
/** How long the in-app toast stays visible (ms) */
const TOAST_AUTO_DISMISS_MS = 15_000;
/** Timer tick interval (ms) */
const TIMER_TICK_MS = 1_000;

// Module-level set to track which notification tags have already fired,
// preventing re-notification when navigating to a workflow page that
// remounts the hook while isWaitingForCode is still true.
const notifiedTags = new Set<string>();

type UseVerificationCodeAlertOptions = {
  isWaitingForCode: boolean;
  pollingStartedAt: string | null | undefined;
  label: string;
  notificationTag: string;
  navigateUrl?: string;
};

type UseVerificationCodeAlertReturn = {
  timeRemaining: number | null;
  isTimeCritical: boolean;
  isTimedOut: boolean;
};

function useVerificationCodeAlert({
  isWaitingForCode,
  pollingStartedAt,
  label,
  notificationTag,
  navigateUrl,
}: UseVerificationCodeAlertOptions): UseVerificationCodeAlertReturn {
  const [timeRemaining, setTimeRemaining] = useState<number | null>(null);
  const toastDismissRef = useRef<(() => void) | null>(null);

  // Countdown timer — reset when waiting state changes
  useEffect(() => {
    if (!isWaitingForCode || !pollingStartedAt) {
      setTimeRemaining(null);
      if (!isWaitingForCode) {
        notifiedTags.delete(notificationTag);
      }
      // Dismiss toast immediately when no longer waiting for code
      toastDismissRef.current?.();
      toastDismissRef.current = null;
      return;
    }

    // Normalize to UTC: backend sends ISO 8601 without timezone suffix
    // (e.g., "2026-02-21T12:00:00.000000" from Python's datetime.utcnow().isoformat())
    // JavaScript interprets timestamps without "Z" as local time, causing incorrect calculations
    let normalizedTimestamp = pollingStartedAt;
    if (
      !pollingStartedAt.endsWith("Z") &&
      !pollingStartedAt.includes("+") &&
      !pollingStartedAt.includes("-", 10)
    ) {
      normalizedTimestamp = pollingStartedAt + "Z";
    }
    const startTime = new Date(normalizedTimestamp).getTime();
    const timeoutMs = VERIFICATION_TIMEOUT_MINS * 60 * TIMER_TICK_MS;

    const updateTimer = () => {
      const now = Date.now();
      const elapsed = now - startTime;
      const remaining = Math.max(0, Math.ceil((timeoutMs - elapsed) / 1000));
      setTimeRemaining(remaining);
    };

    updateTimer();
    const interval = setInterval(updateTimer, TIMER_TICK_MS);
    return () => clearInterval(interval);
  }, [isWaitingForCode, pollingStartedAt, notificationTag]);

  // Browser notification + in-app toast (fire once per waiting transition)
  useEffect(() => {
    if (!enable2faNotifications) return;
    if (!isWaitingForCode || notifiedTags.has(notificationTag)) return;
    notifiedTags.add(notificationTag);

    // OS-level browser notification
    if (typeof Notification !== "undefined") {
      if (Notification.permission === "default") {
        Notification.requestPermission();
      }
      if (Notification.permission === "granted") {
        try {
          const notification = new Notification("2FA Code Required", {
            body: `${label} needs a verification code to continue.`,
            icon: "/favicon.png",
            tag: notificationTag,
            requireInteraction: true,
          });
          notification.onclick = () => {
            window.focus();
            notification.close();
          };
        } catch (e) {
          console.error("Failed to create notification:", e);
        }
      }
    }

    // In-app toast — uses VerificationToastContent for clean JSX rendering
    const result = toast({
      variant: "default",
      className: "border-warning/50",
      duration: TOAST_AUTO_DISMISS_MS,
      description: createElement(VerificationToastContent, {
        label,
        navigateUrl,
      }),
    });

    toastDismissRef.current = result.dismiss;
  }, [isWaitingForCode, label, notificationTag, navigateUrl]);

  const isTimeCritical =
    timeRemaining !== null && timeRemaining <= CRITICAL_TIME_THRESHOLD_SEC;
  const isTimedOut = timeRemaining !== null && timeRemaining <= 0;

  return { timeRemaining, isTimeCritical, isTimedOut };
}

export { useVerificationCodeAlert };
export type { UseVerificationCodeAlertOptions, UseVerificationCodeAlertReturn };
