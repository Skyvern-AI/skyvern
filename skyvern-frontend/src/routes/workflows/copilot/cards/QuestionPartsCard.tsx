import { CheckIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import type { QuestionInteraction } from "../workflowCopilotTypes";
import { hasAnswer } from "./questionAnswers";
import { MAX_KEYED_CHOICES } from "./keyedChoice";

const TYPED_BUBBLE =
  "max-w-full self-start whitespace-pre-wrap break-words rounded-[10px] border border-border bg-slate-elevation4 dark:border-white/5 px-2.5 py-1.5 text-[12.5px] leading-[1.45] text-foreground";

// The read-only record of a question, laid out like the tray it was answered in. A pending
// question is answered in the composer's QuestionTray; this is what the transcript shows after.
export function QuestionPartsCard({
  interaction,
}: {
  interaction: QuestionInteraction;
}) {
  const answers = interaction.response?.answers ?? [];
  const submitted = new Map(answers.map((answer) => [answer.part_id, answer]));
  const total = interaction.parts.length;
  const resolved = interaction.status === "resolved";
  const skippedAll = resolved && Boolean(interaction.response?.skipped);
  // A reply sent as a message answers the whole question, not one part of it.
  const repliedWhole =
    resolved && !answers.some(hasAnswer) && interaction.response?.text != null;
  const answered = resolved && !skippedAll;
  return (
    <div
      role="group"
      aria-label="Question record"
      data-interaction-id={interaction.interaction_id}
      className="flex min-w-0 flex-col"
    >
      {interaction.parts.map((part, index) => {
        const answer = submitted.get(part.part_id);
        const keyed = part.choices.length <= MAX_KEYED_CHOICES;
        return (
          <div
            key={part.part_id}
            data-part-id={part.part_id}
            className={cn(
              "flex min-w-0 flex-col gap-2 p-3",
              index > 0 && "border-t border-border",
            )}
          >
            <div className="flex min-w-0 flex-col gap-0.5">
              {total > 1 ? (
                <span
                  className={cn(
                    "text-[10.5px] font-bold tabular-nums leading-4",
                    answered
                      ? "text-emerald-700 dark:text-emerald-300"
                      : "text-muted-foreground",
                  )}
                >
                  {index + 1} of {total}
                </span>
              ) : null}
              <p className="whitespace-pre-wrap break-words text-[13px] font-medium leading-normal">
                {part.prompt}
              </p>
            </div>
            {part.choices.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {part.choices.map((choice, choiceIndex) => {
                  const selected = answer?.choice_id === choice.choice_id;
                  return (
                    <span
                      key={choice.choice_id}
                      data-selected={selected}
                      className={cn(
                        "flex max-w-full items-start gap-1.5 rounded-md border py-1 pl-1.5 pr-2.5 text-xs",
                        selected
                          ? "border-emerald-400/60 bg-emerald-500/15 font-semibold text-emerald-800 dark:text-emerald-200"
                          : "border-border text-muted-foreground",
                      )}
                    >
                      {selected ? (
                        <>
                          <CheckIcon
                            aria-hidden
                            className="size-4 shrink-0 text-emerald-600 dark:text-emerald-300"
                          />
                          <span className="sr-only">Selected: </span>
                        </>
                      ) : keyed ? (
                        <kbd
                          aria-hidden
                          className="h-4 min-w-4 shrink-0 rounded border border-border px-1 text-center font-mono text-[10px] leading-[14px] text-muted-foreground"
                        >
                          {choiceIndex + 1}
                        </kbd>
                      ) : null}
                      <span className="min-w-0 whitespace-pre-wrap break-words">
                        {choice.text}
                      </span>
                    </span>
                  );
                })}
              </div>
            ) : null}
            {answer?.text ? (
              <p className={TYPED_BUBBLE}>{answer.text}</p>
            ) : answer?.text === "" && answer.choice_id == null ? (
              <p className="text-xs text-muted-foreground">Sent with no text</p>
            ) : answered && !repliedWhole && !hasAnswer(answer) ? (
              <p className="text-xs text-muted-foreground">Skipped</p>
            ) : null}
          </div>
        );
      })}
      {interaction.response?.text ? (
        <div className="flex border-t border-border p-3">
          <p className={TYPED_BUBBLE}>{interaction.response.text}</p>
        </div>
      ) : null}
    </div>
  );
}
