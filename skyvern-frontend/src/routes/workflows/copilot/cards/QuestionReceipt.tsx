import { useState } from "react";
import {
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CrossCircledIcon,
  MinusCircledIcon,
  QuestionMarkCircledIcon,
} from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import type { QuestionInteraction } from "../workflowCopilotTypes";
import { QuestionPartsCard } from "./QuestionPartsCard";

function answerSummary(interaction: QuestionInteraction): string {
  const byPart = new Map(
    (interaction.response?.answers ?? []).map((answer) => [
      answer.part_id,
      answer,
    ]),
  );
  const pieces = interaction.parts.flatMap((part) => {
    const answer = byPart.get(part.part_id);
    if (!answer) return [];
    const choice = part.choices.find(
      (item) => item.choice_id === answer.choice_id,
    );
    return [choice?.text, answer.text].filter(
      (piece): piece is string => piece != null && piece !== "",
    );
  });
  if (interaction.response?.text) pieces.push(interaction.response.text);
  return pieces.join(" · ");
}

// Where a question sits in the transcript. While it is pending the answer happens in the
// composer's tray, so this row only points there.
export function QuestionReceipt({
  interaction,
}: {
  interaction: QuestionInteraction;
}) {
  const [open, setOpen] = useState(false);
  const count = interaction.parts.length;
  const noun = count === 1 ? "question" : "questions";

  if (interaction.status === "pending") {
    return (
      <div
        data-interaction-id={interaction.interaction_id}
        className="flex items-center gap-2 rounded-lg border border-dashed border-amber-500/50 px-3 py-2 text-xs text-muted-foreground"
      >
        <QuestionMarkCircledIcon className="size-3.5 shrink-0 text-amber-500" />
        <span className="min-w-0">
          <span className="font-semibold text-amber-700 dark:text-yellow-400">
            Copilot asked {count === 1 ? "a question" : `${count} questions`}
          </span>{" "}
          · answer below
        </span>
      </div>
    );
  }

  const skipped =
    interaction.status === "resolved" && interaction.response?.skipped;
  const summary =
    interaction.status === "resolved" ? answerSummary(interaction) : "";
  const title =
    interaction.status === "cancelled"
      ? "Question cancelled"
      : interaction.status === "interrupted"
        ? "Question interrupted"
        : skipped
          ? `Skipped ${count === 1 ? "the question" : `${count} questions`}`
          : summary || "Response sent";
  const detail =
    interaction.status === "interrupted"
      ? "This question's session ended. Send a new message to continue."
      : null;
  const Icon =
    interaction.status === "resolved" && !skipped
      ? CheckIcon
      : interaction.status === "interrupted"
        ? CrossCircledIcon
        : MinusCircledIcon;

  return (
    <div
      data-interaction-id={interaction.interaction_id}
      className="min-w-0 overflow-hidden rounded-lg border border-border bg-slate-elevation2"
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full min-w-0 items-start gap-2 px-3 py-2 text-left text-xs hover:bg-slate-elevation3 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring"
      >
        <Icon
          aria-hidden
          className={cn(
            "mt-px size-3.5 shrink-0",
            Icon === CheckIcon ? "text-success" : "text-muted-foreground",
          )}
        />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="whitespace-pre-wrap break-words text-foreground">
            {title}
          </span>
          {detail ? (
            <span className="text-muted-foreground">{detail}</span>
          ) : null}
          <span className="text-muted-foreground">
            {open ? "Hide" : "Show"} {noun}
          </span>
        </span>
        {open ? (
          <ChevronDownIcon className="mt-px size-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRightIcon className="mt-px size-3.5 shrink-0 text-muted-foreground" />
        )}
      </button>
      {open ? (
        <div className="border-t border-border">
          <QuestionPartsCard interaction={interaction} />
        </div>
      ) : null}
    </div>
  );
}
