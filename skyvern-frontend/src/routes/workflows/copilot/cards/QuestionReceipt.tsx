import { useState } from "react";
import {
  ChevronRightIcon,
  QuestionMarkCircledIcon,
} from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import { parseUtcIsoMs } from "../narrativeState";
import type { QuestionInteraction } from "../workflowCopilotTypes";
import { AttentionMarker } from "./AttentionTray";
import { QuestionPartsCard } from "./QuestionPartsCard";
import { answeredPartIds } from "./questionAnswers";
import { TONE_CLASSES, type Tone } from "./receiptTone";

function questions(count: number): string {
  return count === 1 ? "1 question" : `${count} questions`;
}

function formatAnsweredAt(value: string | null): string | null {
  const ms = parseUtcIsoMs(value);
  if (ms === null) return null;
  return new Date(ms).toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
  });
}

function receiptHeading(interaction: QuestionInteraction): {
  tone: Tone;
  title: string;
  meta: string | null;
} {
  const total = interaction.parts.length;
  if (interaction.status === "cancelled") {
    return { tone: "neutral", title: "Question cancelled", meta: null };
  }
  if (interaction.status === "interrupted") {
    return { tone: "error", title: "Question interrupted", meta: null };
  }
  const at = formatAnsweredAt(interaction.resolved_at);
  if (interaction.response?.skipped) {
    return {
      tone: "neutral",
      title: `You skipped ${total === 1 ? "the question" : questions(total)}`,
      meta: at,
    };
  }
  const answered = answeredPartIds(interaction).size;
  if (answered === 0) {
    return { tone: "answered", title: "You replied", meta: at };
  }
  if (answered === total) {
    return {
      tone: "answered",
      title: `You answered ${questions(total)}`,
      meta: at,
    };
  }
  const skipped = `${total - answered} skipped`;
  return {
    tone: "answered",
    title: `You answered ${answered} of ${questions(total)}`,
    meta: at ? `${skipped} · ${at}` : skipped,
  };
}

// Where a question sits in the transcript. While it is pending the answer happens in the
// composer's tray, so this row only marks the spot.
export function QuestionReceipt({
  interaction,
}: {
  interaction: QuestionInteraction;
}) {
  const [open, setOpen] = useState(false);
  const count = interaction.parts.length;

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
          title={`Copilot asked ${count === 1 ? "a question" : `${count} questions`}`}
        />
      </div>
    );
  }

  const { tone, title, meta } = receiptHeading(interaction);
  const classes = TONE_CLASSES[tone];

  return (
    <div
      data-interaction-id={interaction.interaction_id}
      className={cn("min-w-0 overflow-hidden rounded-lg border", classes.card)}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full min-w-0 items-start gap-2 px-3 py-[11px] text-left text-xs focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring"
      >
        <ChevronRightIcon
          aria-hidden
          className={cn(
            "mt-0.5 size-3.5 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90",
          )}
        />
        <span
          aria-hidden
          className={cn("mt-[5px] size-2 shrink-0 rounded-full", classes.dot)}
        />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5 leading-[18px]">
          <span className="min-w-0 break-words">
            <span className={cn("font-semibold", classes.title)}>{title}</span>
            {meta ? (
              <span className="text-muted-foreground"> · {meta}</span>
            ) : null}
          </span>
          {interaction.status === "interrupted" ? (
            <span className="text-muted-foreground">
              This question's session ended. Send a new message to continue.
            </span>
          ) : null}
        </span>
      </button>
      {open ? (
        <div
          className={cn(
            "border-t bg-slate-elevation2",
            tone === "answered" ? "border-emerald-500/25" : "border-border",
          )}
        >
          <QuestionPartsCard interaction={interaction} />
        </div>
      ) : null}
    </div>
  );
}
