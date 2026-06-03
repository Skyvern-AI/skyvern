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

const toneStyles: Record<PillTone, string> = {
  success: "bg-emerald-600 text-white",
  danger: "bg-rose-600 text-white",
  terminated: "bg-orange-600 text-white",
  running: "bg-blue-600 text-white",
  queued: "bg-amber-500 text-amber-950",
  neutral: "bg-slate-600 text-white",
};

type PillProps = {
  tone: PillTone;
  className?: string;
  children: ReactNode;
};

function Pill({ tone, className, children }: PillProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-medium",
        toneStyles[tone],
        className,
      )}
    >
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
    <Pill tone={tone} className={cn("capitalize", className)}>
      {statusText}
    </Pill>
  );
}

export { StatusBadge, Pill };
export type { PillTone };
