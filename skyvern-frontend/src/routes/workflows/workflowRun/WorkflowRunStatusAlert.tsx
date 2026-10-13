import { BellIcon } from "@radix-ui/react-icons";
import { useEffect, useState } from "react";

import {
  type Status as WorkflowRunStatus,
  type WorkflowRunRetryFields,
} from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ControlTooltip } from "@/routes/workflows/studio/ControlTooltip";
import { useStudioPaneCompact } from "@/routes/workflows/studio/StudioShellContext";
import { cn } from "@/util/utils";

import { runIsRetryWaiting } from "./runRetryState";

const deadStatuses = [
  "completed",
  "failed",
  "terminated",
  "timed_out",
  "canceled",
] as const;

const liveStatuses = ["running", "paused"] as const;

type EndingStatus = (typeof deadStatuses)[number];
type LiveStatus = (typeof liveStatuses)[number];
type WatchableStatus = EndingStatus | LiveStatus;

const statusLabels: Record<WatchableStatus, string> = {
  running: "Running",
  paused: "Paused",
  completed: "Completed",
  failed: "Failed",
  terminated: "Terminated",
  timed_out: "Timed out",
  canceled: "Canceled",
};

const MENU_ITEM_CLASS =
  "cursor-pointer rounded py-1.5 pl-7 pr-3 text-xs font-medium text-muted-foreground focus:text-foreground";

type PermissionState = NotificationPermission | "unsupported";

// Insecure contexts and some mobile browsers ship no Notification constructor
// at all, so every read goes through here rather than touching the global.
function readNotificationPermission(): PermissionState {
  return typeof window !== "undefined" && "Notification" in window
    ? Notification.permission
    : "unsupported";
}

interface Props {
  run: { status: WorkflowRunStatus } & WorkflowRunRetryFields;
  title?: string;
  visible: boolean;
}

function WorkflowRunStatusAlert({ run, title, visible }: Props) {
  const { status, retry_pending } = run;
  const compact = useStudioPaneCompact();
  const [statusesWatched, setStatusesWatched] = useState<Set<WatchableStatus>>(
    new Set(),
  );
  const [permission, setPermission] = useState<PermissionState>(
    readNotificationPermission,
  );

  const hasAllEndingStatuses = deadStatuses.every((s) =>
    statusesWatched.has(s),
  );

  useEffect(() => {
    if (runIsRetryWaiting(run)) return;
    if (permission !== "granted") {
      return;
    }

    if (!statusesWatched.has(status as WatchableStatus)) {
      return;
    }

    const audio = new Audio("/dragon-cry.mp3");

    audio.play().catch((error) => {
      console.error("Failed to play notification sound:", error);
    });

    try {
      const notification = new Notification(
        `Agent Run Status Change: ${status}`,
        {
          body: `The agent run "${title ?? "unknown"}" has changed to status: ${status}`,
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, retry_pending]);

  // Re-read on every open so a permission the user changed in browser settings
  // takes effect without a reload.
  function syncPermission() {
    const current = readNotificationPermission();

    if (current === "default") {
      Notification.requestPermission().then(setPermission);
      return;
    }

    setPermission(current);
  }

  function toggleStatus(watched: WatchableStatus, checked: boolean) {
    setStatusesWatched((prev) => {
      const next = new Set(prev);

      if (checked) {
        next.add(watched);
      } else {
        next.delete(watched);
      }

      return next;
    });
  }

  function statusItem(watched: WatchableStatus) {
    return (
      <DropdownMenuCheckboxItem
        key={watched}
        className={MENU_ITEM_CLASS}
        checked={statusesWatched.has(watched)}
        // Keep the menu open: picking statuses is a multi-select, and Radix
        // closes on select by default.
        onSelect={(event) => event.preventDefault()}
        onCheckedChange={(checked) => toggleStatus(watched, checked === true)}
      >
        {statusLabels[watched]}
      </DropdownMenuCheckboxItem>
    );
  }

  if (!visible) {
    return null;
  }

  const watchedCount = statusesWatched.size;
  const watchedSummary = [
    ...liveStatuses
      .filter((s) => statusesWatched.has(s))
      .map((s) => statusLabels[s].toLowerCase()),
    ...(hasAllEndingStatuses
      ? ["finished"]
      : deadStatuses
          .filter((s) => statusesWatched.has(s))
          .map((s) => statusLabels[s].toLowerCase())),
  ].join(", ");

  return (
    <DropdownMenu
      onOpenChange={(open) => {
        if (open) {
          syncPermission();
        }
      }}
    >
      <ControlTooltip
        content={
          <span className="block max-w-[260px]">
            {watchedCount > 0
              ? `Plays a sound and shows a desktop notification when this run is ${watchedSummary}.`
              : "Play a sound and show a desktop notification when this run reaches a status you pick."}
          </span>
        }
      >
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            aria-label={
              watchedCount > 0
                ? `Notify, ${watchedCount} ${watchedCount === 1 ? "status" : "statuses"} selected`
                : "Notify"
            }
            className={cn(
              "h-7 shrink-0 gap-1.5 px-1.5 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground",
              watchedCount > 0 && "text-foreground",
            )}
          >
            <BellIcon className="h-3.5 w-3.5" aria-hidden />
            {compact ? null : "Notify"}
            {watchedCount > 0 ? (
              <span
                aria-hidden
                className="inline-flex h-4 min-w-[1rem] items-center justify-center rounded bg-foreground px-1 text-[10px] font-semibold tabular-nums text-background"
              >
                {watchedCount}
              </span>
            ) : null}
          </Button>
        </DropdownMenuTrigger>
      </ControlTooltip>
      <DropdownMenuContent align="end" sideOffset={6} className="w-56">
        <DropdownMenuLabel className="px-2 py-1.5 text-[11px] font-normal text-muted-foreground">
          Notify me when this run is
        </DropdownMenuLabel>
        {liveStatuses.map(statusItem)}
        <DropdownMenuSeparator />
        <DropdownMenuCheckboxItem
          className={MENU_ITEM_CLASS}
          checked={hasAllEndingStatuses}
          onSelect={(event) => event.preventDefault()}
          onCheckedChange={(checked) => {
            setStatusesWatched((prev) => {
              const next = new Set(prev);

              deadStatuses.forEach((s) =>
                checked ? next.add(s) : next.delete(s),
              );

              return next;
            });
          }}
        >
          Finished (any outcome)
        </DropdownMenuCheckboxItem>
        {deadStatuses.map(statusItem)}
        {permission === "granted" ? null : (
          <>
            <DropdownMenuSeparator />
            <p className="px-2 py-1.5 text-[11px] leading-snug text-muted-foreground">
              {permission === "denied"
                ? "Desktop notifications are blocked for this site. Allow them in your browser settings to be notified."
                : permission === "unsupported"
                  ? "This browser cannot show desktop notifications."
                  : "Allow desktop notifications when your browser asks."}
            </p>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { WorkflowRunStatusAlert };
