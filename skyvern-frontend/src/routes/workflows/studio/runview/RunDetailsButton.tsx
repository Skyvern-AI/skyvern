import { InfoCircledIcon } from "@radix-ui/react-icons";
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
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";

type RunDetailsButtonProps = {
  workflowRunId: string;
  status: Status;
  startedAt: string | null;
  finishedAt: string | null;
  failureReason: string | null;
  failureCategory: Array<FailureCategory> | null;
  browserSessionId: string | null;
  browserProfileId: string | null;
};

function DetailRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <span className="text-xs text-muted-foreground">{label}</span>
      <div className="min-w-0 text-right text-sm">{children}</div>
    </div>
  );
}

export function RunDetailsButton({
  workflowRunId,
  status,
  startedAt,
  finishedAt,
  failureReason,
  failureCategory,
  browserSessionId,
  browserProfileId,
}: RunDetailsButtonProps) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium",
            "text-muted-foreground hover:bg-slate-elevation3 hover:text-foreground",
            "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          )}
        >
          <InfoCircledIcon className="h-4 w-4" />
          Details
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="flex w-[24rem] max-w-[90vw] flex-col gap-3"
      >
        <DetailRow label="Run ID">
          <div className="flex items-center justify-end gap-1">
            <span className="truncate font-mono text-xs">{workflowRunId}</span>
            <CopyButton value={workflowRunId} />
          </div>
        </DetailRow>
        <DetailRow label="Status">
          <StatusBadge status={status} />
        </DetailRow>
        {failureCategory?.length ? (
          <DetailRow label="Failure">
            <FailureCategoryBadge failureCategory={failureCategory} />
          </DetailRow>
        ) : null}
        {failureReason ? (
          <DetailRow label="Reason">
            <span className="whitespace-pre-wrap break-words text-destructive">
              {failureReason}
            </span>
          </DetailRow>
        ) : null}
        {startedAt ? (
          <DetailRow label="Started">
            <span title={basicTimeFormat(startedAt)}>
              {basicLocalTimeFormat(startedAt)}
            </span>
          </DetailRow>
        ) : null}
        {finishedAt ? (
          <DetailRow label="Finished">
            <span title={basicTimeFormat(finishedAt)}>
              {basicLocalTimeFormat(finishedAt)}
            </span>
          </DetailRow>
        ) : null}
        {browserSessionId ? (
          <DetailRow label="Browser session">
            <Link
              to={`/browser-session/${browserSessionId}/stream`}
              className="truncate font-mono text-xs underline underline-offset-4"
            >
              {browserSessionId}
            </Link>
          </DetailRow>
        ) : null}
        {browserProfileId ? (
          <DetailRow label="Browser profile">
            <Link
              to={`/browser-profiles/${browserProfileId}`}
              className="truncate font-mono text-xs underline underline-offset-4"
            >
              {browserProfileId}
            </Link>
          </DetailRow>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
