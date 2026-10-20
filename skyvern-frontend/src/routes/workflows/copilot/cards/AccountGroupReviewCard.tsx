import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { cn } from "@/util/utils";
import type {
  AccountGroupCancelReview,
  AccountGroupOutcome,
  AccountGroupReview,
} from "../workflowCopilotTypes";
import { AttentionTray } from "./AttentionTray";

const PRIOR_EFFECT: Partial<Record<AccountGroupOutcome, string>> = {
  completed: "already completed last time",
  unknown: "may have acted last time",
  pending: "waiting in another group",
  in_progress: "running in another group",
};

function plural(count: number, noun: string): string {
  return count === 1 ? `1 ${noun}` : `${count} ${noun}s`;
}

function inputText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function AccountGroupReviewCard({
  review,
  disabled,
  lockReason,
  collapsed,
  onCollapsedChange,
  onApprove,
  onDecline,
  upNext,
}: {
  review: AccountGroupReview;
  disabled: boolean;
  lockReason: string | null;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  onApprove: (credentialIds: string[]) => void;
  onDecline: () => void;
  upNext?: string | null;
}) {
  const titleId = useId();
  const [checked, setChecked] = useState(
    () =>
      new Set(
        review.rows
          .filter((row) => row.preselected)
          .map((row) => row.credential_id),
      ),
  );
  const toggle = (credentialId: string) =>
    setChecked((current) => {
      const next = new Set(current);
      if (!next.delete(credentialId)) next.add(credentialId);
      return next;
    });
  const approved = review.rows
    .map((row) => row.credential_id)
    .filter((credentialId) => checked.has(credentialId));
  const inputs = Object.entries(review.common_inputs);

  return (
    <AttentionTray
      aria-label="Account review"
      aria-describedby={titleId}
      title="Review the accounts before anything runs"
      titleId={titleId}
      wrapTitle
      collapsedTitle={`Copilot is waiting on your review of ${plural(review.rows.length, "account")}`}
      collapsed={collapsed}
      onCollapsedChange={onCollapsedChange}
      minimizeLabel="Minimize account review"
      upNext={upNext}
    >
      <div className="flex min-h-0 flex-col gap-2 overflow-y-auto px-3 pb-2.5 pt-1 text-xs">
        <p className="whitespace-pre-wrap break-words text-[13.5px] font-medium leading-relaxed">
          {review.action_summary}
        </p>
        <p className="text-muted-foreground">
          {review.workflow_title} · version {review.version} · runs one account
          at a time, each in a fresh browser, using the account's saved login as
          it is when that run starts
        </p>
        {review.credential_parameter_used_by?.length === 0 ? (
          <p className="text-amber-700 dark:text-yellow-400">
            No step of this workflow lists {review.credential_parameter_key}, so
            each run may not sign in as its own account.
          </p>
        ) : null}
        {review.clean_group_run_id ? null : (
          <p className="text-amber-700 dark:text-yellow-400">
            This version has not yet completed cleanly in a group for any
            selected account.
          </p>
        )}
        {inputs.length > 0 ? (
          <dl className="grid grid-cols-[auto,1fr] gap-x-2 gap-y-0.5">
            {inputs.map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="text-muted-foreground">{key}</dt>
                <dd className="break-words">{inputText(value)}</dd>
              </div>
            ))}
          </dl>
        ) : null}
        <ul className="flex flex-col gap-1">
          {review.rows.map((row) => {
            const warning = row.prior_outcome
              ? PRIOR_EFFECT[row.prior_outcome]
              : undefined;
            return (
              <li key={row.credential_id}>
                <label
                  className={cn(
                    "flex cursor-pointer items-start gap-2",
                    disabled && "cursor-not-allowed opacity-60",
                  )}
                >
                  <Checkbox
                    className="mt-0.5"
                    disabled={disabled}
                    checked={checked.has(row.credential_id)}
                    onCheckedChange={() => toggle(row.credential_id)}
                  />
                  <span className="min-w-0 break-words">
                    {row.label}
                    {warning ? (
                      <span className="text-amber-700 dark:text-yellow-400">
                        {" "}
                        · {warning}
                      </span>
                    ) : null}
                  </span>
                </label>
              </li>
            );
          })}
        </ul>
      </div>
      <div
        className="flex shrink-0 flex-wrap items-center gap-2 border-t border-border px-3 py-1.5"
        title={lockReason ?? undefined}
      >
        <Button
          size="sm"
          className="h-7"
          disabled={disabled || approved.length === 0}
          onClick={() => onApprove(approved)}
        >
          Approve {plural(approved.length, "run")}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="h-7"
          disabled={disabled}
          onClick={onDecline}
        >
          Decline
        </Button>
        {lockReason ? (
          <span className="min-w-0 truncate text-[11px] text-muted-foreground">
            {lockReason}
          </span>
        ) : null}
      </div>
    </AttentionTray>
  );
}

export function AccountGroupCancelCard({
  review,
  disabled,
  lockReason,
  collapsed,
  onCollapsedChange,
  onDecide,
  upNext,
}: {
  review: AccountGroupCancelReview;
  disabled: boolean;
  lockReason: string | null;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  onDecide: (approved: boolean) => void;
  upNext?: string | null;
}) {
  const titleId = useId();
  const count = review.unfinished_rows.length;
  return (
    <AttentionTray
      aria-label="Cancel review"
      aria-describedby={titleId}
      title={`Stop ${plural(count, "account run")} that ${count === 1 ? "has" : "have"} not finished?`}
      titleId={titleId}
      wrapTitle
      collapsedTitle={`Copilot is waiting on your answer about stopping ${plural(count, "run")}`}
      collapsed={collapsed}
      onCollapsedChange={onCollapsedChange}
      minimizeLabel="Minimize cancel review"
      upNext={upNext}
    >
      <div className="flex min-h-0 flex-col gap-2 overflow-y-auto px-3 pb-2.5 pt-1 text-xs">
        <p className="text-muted-foreground">
          Stopping cancels every run listed here. A run already in progress may
          have acted before it stops. Finished runs keep their results.
        </p>
        <ul className="flex flex-col gap-1">
          {review.unfinished_rows.map((row) => (
            <li key={row.credential_id} className="break-words">
              {row.label}
              <span className="text-muted-foreground">
                {" "}
                · {row.outcome === "in_progress" ? "running" : "waiting"}
              </span>
            </li>
          ))}
        </ul>
      </div>
      <div
        className="flex shrink-0 flex-wrap items-center gap-2 border-t border-border px-3 py-1.5"
        title={lockReason ?? undefined}
      >
        <Button
          size="sm"
          variant="destructive"
          className="h-7"
          disabled={disabled}
          onClick={() => onDecide(true)}
        >
          Stop {plural(count, "run")}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="h-7"
          disabled={disabled}
          onClick={() => onDecide(false)}
        >
          Keep running
        </Button>
        {lockReason ? (
          <span className="min-w-0 truncate text-[11px] text-muted-foreground">
            {lockReason}
          </span>
        ) : null}
      </div>
    </AttentionTray>
  );
}
