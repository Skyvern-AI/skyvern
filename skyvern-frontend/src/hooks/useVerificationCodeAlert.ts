import { useState, useEffect, useMemo, useRef } from "react";
import { LockClosedIcon, ExternalLinkIcon } from "@radix-ui/react-icons";
import { Link } from "react-router-dom";
import { toast } from "@/components/ui/use-toast";
import { createElement } from "react";
import { enable2faNotifications } from "@/util/env";

const VERIFICATION_CODE_TIMEOUT_MINS = 15;

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
  const autoDismissTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );

  // Countdown timer — reset when waiting state changes
  useEffect(() => {
    if (!isWaitingForCode || !pollingStartedAt) {
      setTimeRemaining(null);
      notifiedTags.delete(notificationTag);
      // Dismiss toast immediately when no longer waiting for code
      toastDismissRef.current?.();
      toastDismissRef.current = null;
      if (autoDismissTimerRef.current) {
        clearTimeout(autoDismissTimerRef.current);
        autoDismissTimerRef.current = null;
      }
      return;
    }

    const startTime = new Date(pollingStartedAt).getTime();
    const timeoutMs = VERIFICATION_CODE_TIMEOUT_MINS * 60 * 1000;

    const updateTimer = () => {
      const now = Date.now();
      const elapsed = now - startTime;
      const remaining = Math.max(0, Math.ceil((timeoutMs - elapsed) / 1000));
      setTimeRemaining(remaining);
    };

    updateTimer();
    const interval = setInterval(updateTimer, 1000);
    return () => clearInterval(interval);
  }, [isWaitingForCode, pollingStartedAt, notificationTag]);

  // Browser notification + sound + in-app toast (fire once per waiting transition)
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

    // Sound alert
    try {
      const audio = new Audio("/dragon-cry.mp3");
      audio.play().catch((e) => console.error("Failed to play sound:", e));
    } catch (e) {
      console.error("Failed to create audio:", e);
    }

    // In-app toast — Figma Option C style (dark bg, amber outline, lock icon, nav link)
    const result = toast({
      variant: "default",
      className: "border-warning/50",
      title: createElement(
        "div",
        { className: "flex items-start gap-2" },
        createElement(LockClosedIcon, {
          className: "mt-0.5 h-4 w-4 flex-shrink-0 text-warning",
        }),
        createElement("span", null, "2FA Code Required"),
      ),
      description: createElement(
        "div",
        { className: "space-y-2" },
        createElement(
          "p",
          { className: "text-muted-foreground" },
          `${label} needs verification to continue.`,
        ),
        navigateUrl
          ? createElement(
              Link,
              {
                to: navigateUrl,
                className:
                  "inline-flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300",
              },
              "Go to workflow",
              createElement(ExternalLinkIcon, { className: "h-3 w-3" }),
            )
          : null,
      ),
    });

    toastDismissRef.current = result.dismiss;
    autoDismissTimerRef.current = setTimeout(() => {
      result.dismiss();
      autoDismissTimerRef.current = null;
    }, 15_000);

    return () => {
      if (autoDismissTimerRef.current) {
        clearTimeout(autoDismissTimerRef.current);
        autoDismissTimerRef.current = null;
      }
    };
  }, [isWaitingForCode, label, notificationTag, navigateUrl]);

  const isTimeCritical = useMemo(
    () => timeRemaining !== null && timeRemaining <= 60,
    [timeRemaining],
  );
  const isTimedOut = useMemo(
    () => timeRemaining !== null && timeRemaining <= 0,
    [timeRemaining],
  );

  return { timeRemaining, isTimeCritical, isTimedOut };
}

export { useVerificationCodeAlert };
export type { UseVerificationCodeAlertOptions, UseVerificationCodeAlertReturn };
