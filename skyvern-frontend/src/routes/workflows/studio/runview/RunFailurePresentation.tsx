import { type ReactNode } from "react";
import {
  CrossCircledIcon,
  MagicWandIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";

import { type WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
import { runIsRetryWaiting } from "@/routes/workflows/workflowRun/runRetryState";
import { Button } from "@/components/ui/button";

import { type RunErrorCode } from "../runProjections";
// Fix leads when it is offered; Retry is the primary only when it stands alone.
export function FailureRecoveryActions({
  onFix,
  onRetry,
}: {
  onFix?: () => void;
  onRetry?: () => void;
}) {
  if (!onFix && !onRetry) {
    return null;
  }
  return (
    <>
      {onFix ? (
        <Button size="sm" onClick={onFix}>
          <MagicWandIcon className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />
          Fix with Copilot
        </Button>
      ) : null}
      {onRetry ? (
        <Button
          size="sm"
          variant={onFix ? "secondary" : "default"}
          onClick={onRetry}
        >
          <ReloadIcon className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />
          Retry
        </Button>
      ) : null}
    </>
  );
}

export function FailureTips({ tips }: { tips: Array<string> }) {
  return (
    <>
      {tips.map((tip) => (
        <span
          key={tip}
          className="basis-full text-[11px] italic text-muted-foreground"
        >
          {tip}
        </span>
      ))}
    </>
  );
}

// Past this many the codes would own the line; the rest stay one click away in
// Outputs → Errors, which lists every code with its message.
const MAX_FAILURE_LINE_ERROR_CODES = 3;

// Verbatim, because customers match on the exact code — same text as
// Outputs → Errors, so neither surface has to be translated to the other.
export function RunFailureErrorCodes({
  codes,
}: {
  codes: Array<RunErrorCode>;
}) {
  const shown = codes.slice(0, MAX_FAILURE_LINE_ERROR_CODES);
  const hidden = codes.length - shown.length;
  return (
    <>
      {shown.map((entry) => (
        <code
          key={entry.code}
          title={entry.message ?? undefined}
          className="shrink-0 rounded border border-destructive/40 bg-destructive/10 px-1.5 py-0.5 font-mono text-[11px] leading-4 text-destructive"
        >
          {entry.code}
        </code>
      ))}
      {hidden > 0 ? (
        <span
          className="shrink-0 text-[11px] text-muted-foreground"
          title="Outputs → Errors lists every error code for this run."
        >
          +{hidden} more
        </span>
      ) : null}
    </>
  );
}

// The strip's second line. The block name is the jump; the full
// reason rides the headline's title so nothing else is said at run level.
export function RunFailureLine({
  workflowRun,
  blockLabel,
  errorCodes,
  headline,
  detail,
  onJump,
  tips,
  children,
}: {
  workflowRun?: WorkflowRunStatusApiResponseWithWorkflow;
  blockLabel: string | null;
  errorCodes: Array<RunErrorCode>;
  headline: string;
  detail: string | null;
  onJump?: () => void;
  tips: Array<string>;
  children: ReactNode;
}) {
  if (workflowRun && runIsRetryWaiting(workflowRun)) {
    return (
      <div className="shrink-0 text-xs text-muted-foreground">
        Retry pending. Skyvern will run this workflow again.
      </div>
    );
  }
  return (
    <div
      data-testid="run-failure-line"
      className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1.5 text-xs"
    >
      <CrossCircledIcon
        className="size-3.5 shrink-0 text-destructive"
        aria-hidden="true"
      />
      {blockLabel ? (
        <button
          type="button"
          onClick={onJump}
          className="shrink-0 font-medium text-foreground underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          {blockLabel}
        </button>
      ) : null}
      <RunFailureErrorCodes codes={errorCodes} />
      <span
        className="min-w-0 flex-auto truncate text-foreground"
        title={detail ?? undefined}
      >
        {blockLabel ? `— ${headline}` : headline}
      </span>
      <div className="ml-auto flex shrink-0 items-center gap-1.5">
        {children}
      </div>
      <FailureTips tips={tips} />
    </div>
  );
}
