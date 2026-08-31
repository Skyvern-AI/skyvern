import { type ReactNode } from "react";
import {
  CrossCircledIcon,
  MagicWandIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
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

// The strip's second line. The block name is the jump; the full
// reason rides the headline's title so nothing else is said at run level.
export function RunFailureLine({
  blockLabel,
  headline,
  detail,
  onJump,
  tips,
  children,
}: {
  blockLabel: string | null;
  headline: string;
  detail: string | null;
  onJump?: () => void;
  tips: Array<string>;
  children: ReactNode;
}) {
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
