import { QuestionMarkCircledIcon } from "@radix-ui/react-icons";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { cn } from "@/util/utils";
import type {
  CredentialDeleteOutcomeKind,
  CredentialDeleteReview,
  CredentialDeleteRow,
  QuestionInteraction,
} from "../workflowCopilotTypes";
import { Heading } from "./AccountGroupReceiptCard";
import { AttentionMarker, AttentionTray } from "./AttentionTray";
import { TONE_CLASSES } from "./receiptTone";

const TYPE_LABEL: Record<CredentialDeleteRow["credential_type"], string> = {
  password: "Password",
  credit_card: "Card",
  secret: "Secret",
};

const OUTCOME_LABEL: Record<CredentialDeleteOutcomeKind, string> = {
  deleted: "Deleted",
  not_found: "Already gone",
  failed: "Failed, may still be saved",
};

function plural(count: number, noun: string): string {
  return count === 1 ? `1 ${noun}` : `${count} ${noun}s`;
}

function scopeText(review: CredentialDeleteReview): string {
  const listed = review.rows.length;
  const total = review.total_credential_count;
  if (listed >= total) {
    return `This card lists all ${plural(total, "saved credential")} in your organization.`;
  }
  return `This card lists ${listed} of your ${plural(total, "saved credential")}. The other ${total - listed} are not on it and stay saved. Ask Copilot for another card to delete the rest.`;
}

export function CredentialDeleteReviewCard({
  review,
  disabled,
  deleting,
  lockReason,
  collapsed,
  onCollapsedChange,
  onConfirm,
  onCancel,
  upNext,
}: {
  review: CredentialDeleteReview;
  disabled: boolean;
  deleting: boolean;
  lockReason: string | null;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  onConfirm: (credentialIds: string[]) => void;
  onCancel: () => void;
  upNext?: string | null;
}) {
  const titleId = useId();
  const [checked, setChecked] = useState(
    () => new Set(review.rows.map((row) => row.credential_id)),
  );
  const locked = disabled || deleting;
  const toggle = (credentialId: string) =>
    setChecked((current) => {
      const next = new Set(current);
      if (!next.delete(credentialId)) next.add(credentialId);
      return next;
    });
  const selected = review.rows
    .map((row) => row.credential_id)
    .filter((credentialId) => checked.has(credentialId));

  return (
    <AttentionTray
      aria-label="Credential deletion"
      aria-describedby={titleId}
      title="Delete saved credentials?"
      titleId={titleId}
      wrapTitle
      collapsedTitle={
        deleting
          ? `Deleting ${plural(review.approved_credential_ids?.length ?? selected.length, "credential")}…`
          : `Copilot is waiting on your answer about deleting ${plural(review.rows.length, "credential")}`
      }
      collapsed={collapsed}
      onCollapsedChange={onCollapsedChange}
      minimizeLabel="Minimize credential deletion"
      upNext={upNext}
    >
      <div className="flex min-h-0 flex-col gap-2 overflow-y-auto px-3 pb-2.5 pt-1 text-xs">
        <p className="text-amber-700 dark:text-yellow-400">
          Deleting is permanent and cannot be undone. Only checked entries are
          deleted.
        </p>
        <p className="text-muted-foreground">{scopeText(review)}</p>
        <ul className="flex flex-col gap-1">
          {review.rows.map((row) => (
            <li key={row.credential_id}>
              <label
                className={cn(
                  "flex cursor-pointer items-start gap-2",
                  locked && "cursor-not-allowed opacity-60",
                )}
              >
                <Checkbox
                  className="mt-0.5"
                  disabled={locked}
                  checked={checked.has(row.credential_id)}
                  onCheckedChange={() => toggle(row.credential_id)}
                />
                <span className="min-w-0 break-words">
                  {row.name}
                  <span className="text-muted-foreground">
                    {" "}
                    · {TYPE_LABEL[row.credential_type]} · {row.credential_id}
                  </span>
                </span>
              </label>
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
          disabled={locked || selected.length === 0}
          onClick={() => onConfirm(selected)}
        >
          {deleting
            ? "Deleting…"
            : `Delete ${plural(selected.length, "credential")}`}
        </Button>
        <span className="sr-only" aria-live="polite">
          {deleting
            ? "Deleting. Stop is unavailable until the deletion finishes."
            : ""}
        </span>
        <Button
          size="sm"
          variant="ghost"
          className="h-7"
          disabled={locked}
          onClick={onCancel}
        >
          Cancel
        </Button>
        {deleting ? (
          <span className="min-w-0 break-words text-[11px] text-muted-foreground">
            Confirmed. Stop is unavailable until the deletion finishes.
          </span>
        ) : lockReason ? (
          <span className="min-w-0 truncate text-[11px] text-muted-foreground">
            {lockReason}
          </span>
        ) : null}
      </div>
    </AttentionTray>
  );
}

export function CredentialDeleteReceipt({
  interaction,
  review,
}: {
  interaction: QuestionInteraction;
  review: CredentialDeleteReview;
}) {
  const count = review.rows.length;
  if (interaction.status === "pending") {
    return (
      <div data-interaction-id={interaction.interaction_id}>
        <AttentionMarker
          icon={
            <QuestionMarkCircledIcon
              aria-hidden
              className="size-3.5 shrink-0"
            />
          }
          title={
            review.claimed_at
              ? `Deleting ${plural(review.approved_credential_ids?.length ?? count, "credential")}…`
              : `Copilot asked to delete ${plural(count, "saved credential")}`
          }
        />
      </div>
    );
  }
  const outcomes = review.outcomes;
  if (!outcomes) {
    const interrupted = interaction.status === "interrupted";
    return (
      <div
        data-interaction-id={interaction.interaction_id}
        className={cn(
          "flex flex-col gap-1.5 rounded-lg border px-3 py-[11px] text-xs",
          TONE_CLASSES[interrupted ? "error" : "neutral"].card,
        )}
      >
        <Heading
          tone={interrupted ? "error" : "neutral"}
          title={
            interaction.status === "resolved"
              ? "You canceled the deletion. Nothing was deleted."
              : "The deletion review ended. Nothing was deleted."
          }
        />
        {interrupted ? (
          <p className="pl-4 text-muted-foreground">
            Send a new message to continue.
          </p>
        ) : null}
      </div>
    );
  }
  const names = new Map(review.rows.map((row) => [row.credential_id, row]));
  const deleted = outcomes.filter((item) => item.outcome === "deleted").length;
  const failed = outcomes.some((item) => item.outcome === "failed");
  const tone = failed
    ? "error"
    : deleted === outcomes.length
      ? "answered"
      : "neutral";
  return (
    <div
      data-interaction-id={interaction.interaction_id}
      className={cn(
        "flex flex-col gap-1.5 rounded-lg border px-3 py-[11px] text-xs",
        TONE_CLASSES[tone].card,
      )}
    >
      <Heading
        tone={tone}
        title={`Deleted ${deleted} of ${plural(outcomes.length, "credential")}`}
      />
      <ul className="flex flex-col gap-0.5 pl-4">
        {outcomes.map((item) => {
          const row = names.get(item.credential_id);
          return (
            <li key={item.credential_id} className="break-words">
              {row?.name ?? item.credential_id}
              {row && (
                <span className="text-muted-foreground">
                  {" "}
                  · {item.credential_id}
                </span>
              )}
              <span
                className={cn(
                  "text-muted-foreground",
                  item.outcome === "failed" &&
                    "text-rose-700 dark:text-rose-300",
                )}
              >
                {" "}
                · {OUTCOME_LABEL[item.outcome]}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
