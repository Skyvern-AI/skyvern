import { QuestionMarkCircledIcon } from "@radix-ui/react-icons";
import { useId } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/util/utils";
import type { QuestionInteraction, SkillWriteReview } from "../emberTypes";
import { Heading } from "./AccountGroupReceiptCard";
import { AttentionMarker, AttentionTray } from "./AttentionTray";
import { TONE_CLASSES } from "./receiptTone";

function effectText(review: SkillWriteReview): string {
  if (review.action === "delete") {
    return `Deletes version ${review.live_version} and every earlier version. Later chats and agent steps in this organization stop getting it.`;
  }
  const replaces =
    review.live_version === null
      ? "This is a new skill."
      : `It replaces version ${review.live_version}; earlier versions stay in its history.`;
  return `Ember in every later chat, and every agent step in this organization, can use it. ${replaces}`;
}

export function SkillWriteReviewCard({
  review,
  disabled,
  lockReason,
  collapsed,
  onCollapsedChange,
  onDecide,
  upNext,
}: {
  review: SkillWriteReview;
  disabled: boolean;
  lockReason: string | null;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  onDecide: (approved: boolean) => void;
  upNext?: string | null;
}) {
  const titleId = useId();
  const verb = review.action === "delete" ? "Delete" : "Save";
  return (
    <AttentionTray
      aria-label="Organization skill review"
      aria-describedby={titleId}
      title={`${verb} the organization skill ${review.name}?`}
      titleId={titleId}
      wrapTitle
      collapsedTitle={`Ember is waiting on your answer about the skill ${review.name}`}
      collapsed={collapsed}
      onCollapsedChange={onCollapsedChange}
      minimizeLabel="Minimize skill review"
      upNext={upNext}
    >
      <div className="flex min-h-0 flex-col gap-2 overflow-y-auto px-3 pb-2.5 pt-1 text-xs">
        <p className="text-muted-foreground">{effectText(review)}</p>
        {review.skill_md ? (
          <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded border border-border bg-muted/40 p-2 font-mono text-[11px]">
            {review.skill_md}
          </pre>
        ) : null}
      </div>
      <div
        className="flex shrink-0 flex-wrap items-center gap-2 border-t border-border px-3 py-1.5"
        title={lockReason ?? undefined}
      >
        <Button
          size="sm"
          variant={review.action === "delete" ? "destructive" : "default"}
          className="h-7"
          disabled={disabled}
          onClick={() => onDecide(true)}
        >
          {verb} skill
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="h-7"
          disabled={disabled}
          onClick={() => onDecide(false)}
        >
          {review.action === "delete" ? "Keep it" : "Don't save"}
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

export function SkillWriteReceipt({
  interaction,
  review,
}: {
  interaction: QuestionInteraction;
  review: SkillWriteReview;
}) {
  const saving = review.action === "save";
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
          title={`Ember asked to ${saving ? "save" : "delete"} the organization skill ${review.name}`}
        />
      </div>
    );
  }
  const approved = review.decision?.approved === true;
  const tone =
    interaction.status === "interrupted"
      ? "error"
      : approved
        ? "answered"
        : "neutral";
  // The card records the answer; the tool's own activity row reports whether the write then succeeded.
  const title = approved
    ? `You approved ${saving ? "saving" : "deleting"} the organization skill ${review.name}`
    : interaction.status === "resolved"
      ? `You declined. The organization skill ${review.name} was not ${saving ? "saved" : "deleted"}.`
      : `The skill review ended. ${review.name} was not ${saving ? "saved" : "deleted"}.`;
  return (
    <div
      data-interaction-id={interaction.interaction_id}
      className={cn(
        "flex flex-col gap-1.5 rounded-lg border px-3 py-[11px] text-xs",
        TONE_CLASSES[tone].card,
      )}
    >
      <Heading tone={tone} title={title} />
    </div>
  );
}
