import { useEffect, useId, useRef, type KeyboardEvent } from "react";
import { CheckIcon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import { cn } from "@/util/utils";
import type { QuestionStepper } from "../useQuestionStepper";
import type { QuestionInteraction } from "../workflowCopilotTypes";
import { AttentionTray } from "./AttentionTray";
import { keyedChoiceFor, MAX_KEYED_CHOICES } from "./keyedChoice";

// One tray renders at a time, so the composer can name the prompt it is answering.
export const QUESTION_PROMPT_ID = "copilot-question-prompt";

export function QuestionTray({
  interaction,
  stepper,
  disabled,
  lockReason,
  collapsed,
  onCollapsedChange,
  onSend,
  onSkip,
  onCancel,
  cancelDisabled,
  cancelTitle,
  upNext,
}: {
  interaction: QuestionInteraction;
  stepper: QuestionStepper;
  disabled: boolean;
  // Why the controls are inert, when that is a hold rather than an in-flight submit. Without it a
  // disabled tray is indistinguishable from a broken one.
  lockReason: string | null;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  onSend: () => void;
  onSkip: () => void;
  onCancel?: () => void;
  cancelDisabled?: boolean;
  cancelTitle?: string;
  upNext?: string | null;
}) {
  const titleId = useId();
  const advanceRef = useRef<HTMLButtonElement>(null);
  // Back unmounts on the first question, so focus moves to Next to keep keyboard users, and the
  // number keys, in the tray.
  const focusAdvance = useRef(false);
  useEffect(() => {
    if (!focusAdvance.current) return;
    focusAdvance.current = false;
    advanceRef.current?.focus();
  }, [stepper.index]);
  const total = interaction.parts.length;
  const part = interaction.parts[stepper.index];
  const noun = total === 1 ? "question" : "questions";

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (disabled || !part || part.choices.length > MAX_KEYED_CHOICES) return;
    const choice = keyedChoiceFor(event, part.choices);
    if (!choice) return;
    event.preventDefault();
    stepper.toggleChoice(part.part_id, choice.choice_id);
  };

  return (
    <AttentionTray
      aria-label="Question parts"
      aria-describedby={titleId}
      data-interaction-id={interaction.interaction_id}
      onKeyDown={onKeyDown}
      title="Copilot needs your answer"
      titleId={titleId}
      meta={
        total > 1 ? (
          <span className="tabular-nums text-muted-foreground">
            {stepper.index + 1} of {total}
          </span>
        ) : null
      }
      collapsedTitle={`Copilot is waiting on ${total} ${noun}`}
      collapsed={collapsed}
      onCollapsedChange={onCollapsedChange}
      minimizeLabel="Minimize question"
      upNext={upNext}
    >
      {/* Stays mounted across steps so Back and Next read the new question to a screen reader
          whose focus is still in the composer. */}
      <span className="sr-only" aria-live="polite">
        {total > 1 && part
          ? `Question ${stepper.index + 1} of ${total}: ${part.prompt}`
          : ""}
      </span>
      {part ? (
        <div
          key={part.part_id}
          data-part-id={part.part_id}
          className="flex min-h-0 flex-col gap-2 overflow-y-auto px-3 pb-2.5 pt-1"
        >
          <p
            id={QUESTION_PROMPT_ID}
            className="whitespace-pre-wrap break-words text-[13.5px] font-medium leading-relaxed"
          >
            {part.prompt}
          </p>
          {part.choices.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {part.choices.map((choice, choiceIndex) => {
                const selected =
                  stepper.choices[part.part_id] === choice.choice_id;
                const keyed = part.choices.length <= MAX_KEYED_CHOICES;
                return (
                  <button
                    key={choice.choice_id}
                    type="button"
                    disabled={disabled}
                    aria-pressed={selected}
                    onClick={() =>
                      stepper.toggleChoice(part.part_id, choice.choice_id)
                    }
                    className={cn(
                      "flex max-w-full items-start gap-1.5 rounded-md border border-border bg-slate-elevation3 py-1 pl-1.5 pr-2.5 text-left text-xs transition-colors hover:border-muted-foreground",
                      "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-default disabled:opacity-60 disabled:hover:border-border",
                      "aria-pressed:border-success aria-pressed:bg-accent",
                    )}
                  >
                    {selected ? (
                      <CheckIcon
                        aria-hidden
                        className="size-4 shrink-0 text-success"
                      />
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
                  </button>
                );
              })}
            </div>
          ) : null}
          {/* Typing does not clear a picked choice, so the invitation to answer instead of the
              choices only shows while none is picked. */}
          {part.choices.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              Type your answer in the message box below.
            </p>
          ) : stepper.choices[part.part_id] === undefined ? (
            <p className="text-xs text-muted-foreground">
              None of these fit? Type your own answer in the message box below.
            </p>
          ) : null}
        </div>
      ) : null}
      <div
        className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 border-t border-border px-3 py-1.5"
        title={lockReason ?? undefined}
      >
        {lockReason ? (
          <span className="min-w-0 truncate text-[11px] text-muted-foreground">
            {lockReason}
          </span>
        ) : onCancel ? (
          <Button
            size="sm"
            variant="ghost"
            className="-ml-2 h-7 px-2 text-xs text-muted-foreground"
            disabled={cancelDisabled}
            title={cancelTitle}
            onClick={onCancel}
          >
            Cancel question
          </Button>
        ) : (
          <span />
        )}
        <div className="ml-auto flex shrink-0 gap-1.5">
          <Button
            size="sm"
            variant="ghost"
            className="h-7"
            disabled={disabled}
            title={lockReason ?? undefined}
            onClick={() => (stepper.isLast ? onSkip() : stepper.skipCurrent())}
          >
            Skip
          </Button>
          {stepper.index > 0 ? (
            <Button
              size="sm"
              variant="outline"
              className="h-7"
              disabled={disabled}
              onClick={() => {
                if (stepper.index === 1) focusAdvance.current = true;
                stepper.goTo(stepper.index - 1);
              }}
            >
              Back
            </Button>
          ) : null}
          {stepper.isLast ? (
            <Button
              ref={advanceRef}
              size="sm"
              className="h-7"
              disabled={disabled || stepper.answeredCount === 0}
              title={lockReason ?? undefined}
              onClick={onSend}
            >
              Send
            </Button>
          ) : (
            <Button
              ref={advanceRef}
              size="sm"
              className="h-7"
              disabled={disabled}
              onClick={() => stepper.goTo(stepper.index + 1)}
            >
              Next
            </Button>
          )}
        </div>
      </div>
    </AttentionTray>
  );
}
