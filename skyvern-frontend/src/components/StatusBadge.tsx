import { type ReactNode } from "react";

import { Status } from "@/api/types";
import { cn } from "@/util/utils";

type PillTone =
  | "success"
  | "danger"
  | "terminated"
  | "running"
  | "queued"
  | "neutral";

const toneStyles: Record<PillTone, { pill: string; dot: string }> = {
  success: {
    pill: "bg-emerald-500/10 text-emerald-700 ring-emerald-600/20 dark:text-emerald-400 dark:ring-emerald-500/25",
    dot: "bg-emerald-500",
  },
  danger: {
    pill: "bg-rose-500/10 text-rose-700 ring-rose-600/20 dark:text-rose-400 dark:ring-rose-500/25",
    dot: "bg-rose-500",
  },
  terminated: {
    pill: "bg-orange-500/10 text-orange-700 ring-orange-600/20 dark:text-orange-400 dark:ring-orange-500/25",
    dot: "bg-orange-500",
  },
  running: {
    pill: "bg-blue-500/10 text-blue-700 ring-blue-600/20 dark:text-blue-400 dark:ring-blue-500/25",
    dot: "bg-blue-500",
  },
  queued: {
    pill: "bg-amber-500/10 text-amber-700 ring-amber-600/20 dark:text-amber-500 dark:ring-amber-500/25",
    dot: "bg-amber-500",
  },
  neutral: {
    pill: "bg-slate-500/10 text-slate-600 ring-slate-500/20 dark:text-slate-300 dark:ring-slate-400/20",
    dot: "bg-slate-400",
  },
};

type PillProps = {
  tone: PillTone;
  pulse?: boolean;
  className?: string;
  children: ReactNode;
};

function Pill({ tone, pulse = false, className, children }: PillProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset",
        toneStyles[tone].pill,
        className,
      )}
    >
      <span
        className={cn(
          "size-1.5 shrink-0 rounded-full",
          toneStyles[tone].dot,
          pulse && "animate-pulse",
        )}
      />
      {children}
    </span>
  );
}

type Props = {
  className?: string;
  status: Status | "pending";
};

function toneForStatus(status: Status | "pending"): PillTone {
  switch (status) {
    case Status.Completed:
      return "success";
    case Status.Failed:
    case Status.Canceled:
    case Status.TimedOut:
      return "danger";
    case Status.Terminated:
      return "terminated";
    case Status.Running:
      return "running";
    case Status.Queued:
    case "pending":
      return "queued";
    case Status.Created:
    default:
      return "neutral";
  }
}

function StatusBadge({ className, status }: Props) {
  const statusText = status === "timed_out" ? "timed out" : status;
  const tone = toneForStatus(status);

  return (
    <Pill
      tone={tone}
      pulse={tone === "running" || tone === "queued"}
      className={cn("capitalize", className)}
    >
      {statusText}
    </Pill>
  );
}

export { StatusBadge, Pill };
export type { PillTone };
