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

import { TerminatedIcon } from "./terminatedVisual";

export type StatusVariant =
  | "success"
  | "warning"
  | "destructive"
  | "terminated"
  | "secondary";

export function variantForStatus(status: Status | "pending"): StatusVariant {
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

export function iconForStatus(
  status: Status | "pending",
  className = "h-3.5 w-3.5 shrink-0",
) {
  switch (status) {
    case Status.Completed:
      return <CheckCircledIcon className={className} />;
    case Status.Running:
      return <UpdateIcon className={className} />;
    case Status.Queued:
    case "pending":
      return <ClockIcon className={className} />;
    case Status.Failed:
      return <CrossCircledIcon className={className} />;
    case Status.Canceled:
      return <CircleBackslashIcon className={className} />;
    case Status.TimedOut:
      return <StopwatchIcon className={className} />;
    case Status.Terminated:
      return <TerminatedIcon className={className} />;
    case Status.Skipped:
      return <MinusCircledIcon className={className} />;
    case Status.Paused:
      return <PauseIcon className={className} />;
    case Status.Created:
    default:
      return <CircleIcon className={className} />;
  }
}
