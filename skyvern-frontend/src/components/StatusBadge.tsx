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

function StatusBadge({ className, status }: Props) {
  const statusText = status === "timed_out" ? "timed out" : status;

  return (
    <Badge
      variant={variantForStatus(status)}
      className={cn(
        "justify-center gap-1.5 px-1.5 capitalize md:w-28 md:justify-start md:px-2.5",
        className,
      )}
      title={statusText}
    >
      {iconForStatus(status)}
      <span className="sr-only md:not-sr-only">{statusText}</span>
    </Badge>
  );
}

export { StatusBadge, Pill };
export type { PillTone };
