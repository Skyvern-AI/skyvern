import {
  BellIcon,
  ChevronDownIcon,
  ChevronUpIcon,
} from "@radix-ui/react-icons";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { HelpTooltip } from "@/components/HelpTooltip";
import { type Status as WorkflowRunStatus } from "@/api/types";

const deadStatuses = [
  "canceled",
  "completed",
  "failed",
  "terminated",
  "timed_out",
] as const;

const liveStatuses = ["paused", "running"] as const;

type EndingStatus = (typeof deadStatuses)[number];
type LiveStatus = (typeof liveStatuses)[number];
type WatchableStatus = EndingStatus | LiveStatus;

interface Props {
  status: WorkflowRunStatus;
  title?: string;
  visible: boolean;
}

function WorkflowRunStatusAlert({ status, title, visible }: Props) {
  const [notifyIsOpen, setNotifyIsOpen] = useState(false);
  const [statusesWatched, setStatusesWatched] = useState<Set<WatchableStatus>>(
    new Set(),
  );
  const [hasRequestedNotification, setHasRequestedNotification] = useState(
    statusesWatched.size > 0,
  );

  const dropdownRef = useRef<HTMLDivElement>(null);

  const hasAllEndingStatuses = deadStatuses.every((s) =>
    statusesWatched.has(s),
  );

  // Handle click outside to close dropdown
  useEffect(() => {
    if (!notifyIsOpen) {
      return;
    }

    function handleClickOutside(event: MouseEvent) {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(event.target as Node)
      ) {
        setNotifyIsOpen(false);
      }
    }

    document.addEventListener("mousedown", handleClickOutside);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, [notifyIsOpen]);

  // Handle ESC key to close dropdown
  useEffect(() => {
    if (!notifyIsOpen) {
      return;
    }

    function handleEscKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setNotifyIsOpen(false);
      }
    }

    document.addEventListener("keydown", handleEscKey);
    return () => {
      document.removeEventListener("keydown", handleEscKey);
    };
  }, [notifyIsOpen]);

  useEffect(() => {
    if (!hasRequestedNotification) {
      return;
    }

    if (!statusesWatched.has(status as WatchableStatus)) {
      return;
    }

    const audio = new Audio("/dragon-cry.mp3");

    audio.play().catch((error) => {
      console.error("Failed to play notification sound:", error);
    });

    if (Notification.permission === "granted") {
      try {
        const notification = new Notification(
          `Workflow Run Status Change: ${status}`,
          {
            body: `The workflow run "${title ?? "unknown"}" has changed to status: ${status}`,
            icon: "/favicon.png",
            tag: `workflow-${title ?? "unknown"}-${status}`,
            requireInteraction: false,
          },
        );

        notification.onclick = () => {
          window.focus();
          notification.close();
        };
      } catch (error) {
        console.error("Failed to create notification:", error);
      }
    } else {
      console.warn(
        "Notification permission not granted:",
        Notification.permission,
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  function askForPermissions() {
    if (hasRequestedNotification) {
      return;
    }

    if (Notification.permission === "default") {
      Notification.requestPermission().then((permission) => {
        if (permission === "granted") {
          setHasRequestedNotification(true);
        }
      });
    } else if (Notification.permission === "granted") {
      setHasRequestedNotification(true);
    }
  }

  const notifySuffix =
    statusesWatched.size === 0
      ? null
      : statusesWatched.size === 1
        ? `(${(Array.from(statusesWatched)[0] ?? "").replace("_", " ")})`
        : statusesWatched.size === deadStatuses.length &&
            deadStatuses.every((s) => statusesWatched.has(s))
          ? "(ending)"
          : statusesWatched.size === liveStatuses.length + deadStatuses.length
            ? "(any)"
            : `(${statusesWatched.size} statuses)`;

  if (!visible) {
    return null;
  }

  return (
    <div ref={dropdownRef} className="relative inline-block">
      <div className="flex items-center gap-2">
        <Button
          className="relative"
          variant="outline"
          onClick={() => {
            askForPermissions();
            setNotifyIsOpen(!notifyIsOpen);
          }}
        >
          <BellIcon className="mr-2 h-4 w-4" />
          {notifySuffix ? `Notify ${notifySuffix}` : "Notify"}
          {notifyIsOpen ? (
            <ChevronUpIcon className="ml-2 h-4 w-4" />
          ) : (
            <ChevronDownIcon className="ml-2 h-4 w-4" />
          )}
          {notifyIsOpen && (
            <div
              className="absolute right-0 top-full z-10 mt-2 w-48 rounded-md border bg-background shadow-lg"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="space-y-1 p-2">
                {liveStatuses.map((s) => (
                  <div key={s} className="flex items-center gap-2 p-2 text-sm">
                    <Checkbox
                      id={s}
                      checked={statusesWatched.has(s)}
                      onCheckedChange={(checked) => {
                        setStatusesWatched((prev) => {
                          const newSet = new Set(prev);
                          if (checked) {
                            newSet.add(s);
                          } else {
                            newSet.delete(s);
                          }
                          return newSet;
                        });
                      }}
                    />
                    <label htmlFor={s} className="cursor-pointer">
                      {s}
                    </label>
                  </div>
                ))}
              </div>
              <div className="mt-2 space-y-1 border-t bg-slate-elevation3 p-2">
                <div
                  key="any-ending"
                  className="flex items-center gap-2 p-2 text-sm"
                >
                  <Checkbox
                    id="any-ending"
                    checked={hasAllEndingStatuses}
                    onCheckedChange={(checked) => {
                      setStatusesWatched((prev) => {
                        const newSet = new Set(prev);

                        if (checked) {
                          deadStatuses.forEach((s) => newSet.add(s));
                        } else if (!checked) {
                          deadStatuses.forEach((s) => newSet.delete(s));
                        }

                        return newSet;
                      });
                    }}
                  />
                  <label htmlFor="any-ending" className="cursor-pointer">
                    all ending
                  </label>
                </div>
                {deadStatuses.map((s) => (
                  <div key={s} className="flex items-center gap-2 p-2 text-sm">
                    <Checkbox
                      id={s}
                      checked={statusesWatched.has(s)}
                      onCheckedChange={(checked) => {
                        setStatusesWatched((prev) => {
                          const newSet = new Set(prev);
                          if (checked) {
                            newSet.add(s);
                          } else {
                            newSet.delete(s);
                          }
                          return newSet;
                        });
                      }}
                    />
                    <label htmlFor={s} className="cursor-pointer">
                      {s.replace("_", " ")}
                    </label>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Button>
        <HelpTooltip content="When this workflow changes to a particular status, notify me via OS notifications and a sound." />
      </div>
    </div>
  );
}

export { WorkflowRunStatusAlert };
