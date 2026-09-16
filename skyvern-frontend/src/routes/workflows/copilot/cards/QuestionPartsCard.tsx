import { useId, useState } from "react";
import { CheckIcon, QuestionMarkCircledIcon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import type {
  QuestionAnswer,
  QuestionInteraction,
  QuestionResponse,
} from "../workflowCopilotTypes";

export function QuestionPartsCard({
  interaction,
  disabled = false,
  onAnswer,
}: {
  interaction: QuestionInteraction;
  disabled?: boolean;
  onAnswer: (response: QuestionResponse) => void;
}) {
  const [answers, setAnswers] = useState<Record<string, QuestionAnswer>>({});
  const fieldId = useId();
  const [freeText, setFreeText] = useState("");
  const readOnly = disabled || interaction.status !== "pending";
  const submitted = Object.fromEntries(
    (interaction.response?.answers ?? []).map((answer) => [
      answer.part_id,
      answer,
    ]),
  );
  const active = interaction.status === "resolved" ? submitted : answers;
  const selectedAnswers = Object.values(answers).filter(
    (answer) =>
      answer.choice_id != null || (answer.text != null && answer.text !== ""),
  );
  return (
    <div
      role="group"
      aria-label="Question parts"
      data-interaction-id={interaction.interaction_id}
      className="min-w-0 overflow-hidden rounded-lg border border-border bg-slate-elevation2"
    >
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <QuestionMarkCircledIcon className="size-4 shrink-0 text-muted-foreground" />
        <span className="text-xs font-semibold">
          {interaction.status === "cancelled"
            ? "Question cancelled"
            : interaction.status === "resolved"
              ? interaction.response?.skipped
                ? "Skipped"
                : "Response sent"
              : interaction.status === "interrupted"
                ? "Question interrupted"
                : "Answer what you can"}
        </span>
      </div>
      <div className="flex max-h-[60vh] flex-col overflow-y-auto p-2">
        {interaction.parts.map((part, index) => {
          const answer = active[part.part_id];
          return (
            <div
              key={part.part_id}
              data-part-id={part.part_id}
              className={`flex min-w-0 flex-col gap-2 px-1 py-2 ${index > 0 ? "border-t border-border" : ""}`}
            >
              <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed">
                {part.prompt}
              </p>
              <div className="flex flex-wrap gap-1.5">
                {part.choices.map((choice) => (
                  <button
                    key={choice.choice_id}
                    type="button"
                    disabled={readOnly}
                    aria-pressed={answer?.choice_id === choice.choice_id}
                    onClick={() =>
                      setAnswers((current) => ({
                        ...current,
                        [part.part_id]: {
                          part_id: part.part_id,
                          choice_id:
                            answer?.choice_id === choice.choice_id
                              ? null
                              : choice.choice_id,
                        },
                      }))
                    }
                    className="flex max-w-full items-start gap-1.5 whitespace-pre-wrap break-words rounded-md border border-border px-2 py-1 text-left text-xs disabled:cursor-default aria-pressed:border-success aria-pressed:bg-accent"
                  >
                    {answer?.choice_id === choice.choice_id ? (
                      <CheckIcon className="size-3.5 shrink-0 text-success" />
                    ) : null}
                    <span className="min-w-0 break-words">{choice.text}</span>
                  </button>
                ))}
              </div>
              {interaction.status === "resolved" ? (
                <p className="whitespace-pre-wrap break-words text-xs text-muted-foreground">
                  {answer?.text ??
                    (answer?.choice_id ? "Selected" : "No choice selected")}
                </p>
              ) : null}
            </div>
          );
        })}
      </div>
      {interaction.status === "pending" ? (
        <div className="border-t border-border px-3 py-2">
          <label htmlFor={fieldId} className="text-xs text-muted-foreground">
            Your response
          </label>
          <textarea
            id={fieldId}
            rows={3}
            value={freeText}
            disabled={disabled}
            placeholder="Type an answer or add details…"
            onChange={(event) => setFreeText(event.target.value)}
            className="mt-1 w-full resize-y rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
        </div>
      ) : null}
      {interaction.status === "interrupted" ? (
        <p className="px-3 py-2 text-xs text-muted-foreground">
          This question's session ended. Send a new message to continue.
        </p>
      ) : null}
      {interaction.response?.text != null ? (
        <p className="whitespace-pre-wrap break-words border-t border-border px-3 py-2 text-sm">
          {interaction.response.text}
        </p>
      ) : null}
      {interaction.status === "pending" ? (
        <div className="flex items-center justify-between gap-2 border-t border-border px-3 py-2">
          <span className="text-[11px] text-muted-foreground">
            {selectedAnswers.length} choices selected
          </span>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="ghost"
              disabled={disabled}
              onClick={() => onAnswer({ skipped: true })}
            >
              Skip
            </Button>
            <Button
              size="sm"
              variant="secondary"
              disabled={
                disabled || (selectedAnswers.length === 0 && freeText === "")
              }
              onClick={() =>
                onAnswer({
                  answers: selectedAnswers,
                  ...(freeText !== "" ? { text: freeText } : {}),
                })
              }
            >
              Send
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
