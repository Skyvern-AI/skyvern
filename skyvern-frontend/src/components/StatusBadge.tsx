import { type ReactNode } from "react";

import {
  CheckCircledIcon,
  CircleBackslashIcon,
  CircleIcon,
  ClockIcon,
  CrossCircledIcon,
  MinusCircledIcon,
  PauseIcon,
  StopwatchIcon,
  UpdateIcon,
} from "@radix-ui/react-icons";

import { Status } from "@/api/types";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";

import { TerminatedIcon } from "./terminatedVisual";
import { Badge } from "./ui/badge";

type StatusVariant =
  | "success"
  | "warning"
  | "destructive"
  | "terminated"
  | "secondary";

type PillTone =
  | "success"
  | "danger"
  | "terminated"
  | "running"
  | "queued"
  | "neutral";

const toneToVariant: Record<PillTone, StatusVariant> = {
  success: "success",
  danger: "destructive",
  terminated: "terminated",
  running: "warning",
  queued: "warning",
  neutral: "secondary",
};

type PillProps = {
  tone: PillTone;
  className?: string;
  children: ReactNode;
};

function Pill({ tone, className, children }: PillProps) {
  return (
    <Badge variant={toneToVariant[tone]} className={className}>
      {children}
    </Badge>
  );
}

type Props = {
  className?: string;
  status: Status | "pending";
  // By default the label is sr-only below md (compact pill in dense tables);
  // set this where the label must stay visible at every width, e.g. a tab chip.
  alwaysShowLabel?: boolean;
  // Collapse to icon-only under container width pressure instead of the viewport
  // md: breakpoint — for independently-resizable panes. Requires an ancestor
  // declaring the `status` container (`[container-type:inline-size]
  // [container-name:status]`).
  collapsible?: boolean;
};

function variantForStatus(status: Status | "pending"): StatusVariant {
  switch (status) {
    case Status.Completed:
      return "success";
    case Status.Failed:
    case Status.Canceled:
    case Status.TimedOut:
      return "destructive";
    case Status.Terminated:
      return "terminated";
    case Status.Running:
    case Status.Queued:
    case "pending":
      return "warning";
    case Status.Created:
    default:
      return "secondary";
  }
}

function iconForStatus(status: Status | "pending") {
  const cls = "h-3.5 w-3.5 shrink-0";
  switch (status) {
    case Status.Completed:
      return <CheckCircledIcon className={cls} />;
    case Status.Running:
      return <UpdateIcon className={cls} />;
    case Status.Queued:
    case "pending":
      return <ClockIcon className={cls} />;
    case Status.Failed:
      return <CrossCircledIcon className={cls} />;
    case Status.Canceled:
      return <CircleBackslashIcon className={cls} />;
    case Status.TimedOut:
      return <StopwatchIcon className={cls} />;
    case Status.Terminated:
      return <TerminatedIcon className={cls} />;
    case Status.Skipped:
      return <MinusCircledIcon className={cls} />;
    case Status.Paused:
      return <PauseIcon className={cls} />;
    case Status.Created:
    default:
      return <CircleIcon className={cls} />;
  }
}

function StatusBadge({
  className,
  status,
  alwaysShowLabel = false,
  collapsible = false,
}: Props) {
  const statusText = status === "timed_out" ? "timed out" : status;

  const badge = (
    <Badge
      variant={variantForStatus(status)}
      className={cn(
        "justify-center gap-1.5 px-1.5 capitalize",
        // Container query (no plugin — Tailwind core arbitrary at-rule variant)
        // so the label returns once the `status` container is wide enough.
        collapsible
          ? "[@container_status_(min-width:384px)]:w-28 [@container_status_(min-width:384px)]:justify-start [@container_status_(min-width:384px)]:px-2.5"
          : "md:w-28 md:justify-start md:px-2.5",
        className,
      )}
      // The collapsible variant surfaces the label via the tooltip below and
      // drops the native title to avoid showing two tooltips at once. role=img
      // guarantees the aria-label is announced when the badge collapses to the
      // icon (a bare div computes to role=generic, which AT may prune).
      title={collapsible ? undefined : statusText}
      role={collapsible ? "img" : undefined}
      aria-label={collapsible ? statusText : undefined}
    >
      {iconForStatus(status)}
      <span
        className={
          alwaysShowLabel
            ? undefined
            : collapsible
              ? "sr-only [@container_status_(min-width:384px)]:not-sr-only"
              : "sr-only md:not-sr-only"
        }
      >
        {statusText}
      </span>
    </Badge>
  );

  if (!collapsible) {
    return badge;
  }

  // Self-contained provider so a non-studio consumer can't crash (Tooltip.Root
  // throws without one). tabIndex makes the trigger keyboard-focusable so the
  // label is reachable on focus, not hover only (WCAG 1.4.13).
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            tabIndex={0}
            className="inline-flex shrink-0 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            {badge}
          </span>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="capitalize">
          {statusText}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { StatusBadge, Pill };
export type { PillTone };
