import { type ReactNode } from "react";
import {
  ClockIcon,
  ExclamationTriangleIcon,
  ReaderIcon,
} from "@radix-ui/react-icons";
import { Link } from "react-router-dom";

import { type FailureCategory, type Status } from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import { FailureCategoryBadge } from "@/components/FailureCategoryBadge";
import { StatusBadge } from "@/components/StatusBadge";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useRunViewStore } from "@/store/RunViewStore";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";

import { OverviewField } from "./OverviewField";

type RunOverviewButtonProps = {
  status: Status;
  elapsed: string;
  startedAt: string | null;
  finishedAt: string | null;
  failureReason: string | null;
  failureCategory: Array<FailureCategory> | null;
  workflowRunId: string;
  browserSessionId: string | null;
  browserProfileId: string | null;
};

function Stat({
  label,
  value,
  title,
  icon,
}: {
  label: string;
  value: string;
  title?: string;
  icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span
        title={title}
        className="flex items-center gap-1.5 text-sm font-medium text-foreground"
      >
        {icon ? <span className="text-muted-foreground">{icon}</span> : null}
        {value}
      </span>
    </div>
  );
}

export function RunOverviewButton({
  status,
  elapsed,
  startedAt,
  finishedAt,
  failureReason,
  failureCategory,
  workflowRunId,
  browserSessionId,
  browserProfileId,
}: RunOverviewButtonProps) {
  const compact = useRunViewStore((s) => s.headerCompact);
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          title={compact ? "Overview" : undefined}
          aria-label="Overview"
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium",
            "text-muted-foreground hover:bg-slate-elevation3 hover:text-foreground",
            "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          )}
        >
          <ReaderIcon className="h-4 w-4" />
          {compact ? null : "Overview"}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="flex w-[26rem] max-w-[90vw] flex-col gap-4"
      >
        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          <div className="flex items-center gap-2">
            <StatusBadge status={status} />
            {failureCategory?.length ? (
              <FailureCategoryBadge failureCategory={failureCategory} />
            ) : null}
          </div>
          <Stat
            label="Duration"
            value={elapsed}
            icon={<ClockIcon className="h-3.5 w-3.5" />}
          />
          {startedAt ? (
            <Stat
              label="Started"
              value={basicLocalTimeFormat(startedAt)}
              title={basicTimeFormat(startedAt)}
            />
          ) : null}
          {finishedAt ? (
            <Stat
              label="Finished"
              value={basicLocalTimeFormat(finishedAt)}
              title={basicTimeFormat(finishedAt)}
            />
          ) : null}
        </div>

        {failureReason ? (
          <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm">
            <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
            <span className="whitespace-pre-wrap break-words text-foreground">
              {failureReason}
            </span>
          </div>
        ) : null}

        <div className="flex flex-col gap-3">
          <OverviewField label="Run ID">
            <div className="flex items-center gap-1.5">
              <span className="truncate font-mono text-xs">
                {workflowRunId}
              </span>
              <CopyButton value={workflowRunId} />
            </div>
          </OverviewField>
          {browserSessionId ? (
            <OverviewField label="Browser session">
              <Link
                to={`/browser-session/${browserSessionId}/stream`}
                className="font-mono text-xs text-studio-accent-2 underline-offset-4 hover:underline"
              >
                {browserSessionId}
              </Link>
            </OverviewField>
          ) : null}
          {browserProfileId ? (
            <OverviewField label="Browser profile">
              <Link
                to={`/browser-profiles/${browserProfileId}`}
                className="font-mono text-xs text-studio-accent-2 underline-offset-4 hover:underline"
              >
                {browserProfileId}
              </Link>
            </OverviewField>
          ) : null}
        </div>
      </PopoverContent>
    </Popover>
  );
}
