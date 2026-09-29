import { CheckIcon } from "@radix-ui/react-icons";
import type { QuestionInteraction } from "../workflowCopilotTypes";

// The read-only record of a question. A pending question is answered in the composer's
// QuestionTray; this card is what the transcript shows once it has an outcome.
export function QuestionPartsCard({
  interaction,
}: {
  interaction: QuestionInteraction;
}) {
  const submitted = Object.fromEntries(
    (interaction.response?.answers ?? []).map((answer) => [
      answer.part_id,
      answer,
    ]),
  );
  return (
    <div
      role="group"
      aria-label="Question record"
      data-interaction-id={interaction.interaction_id}
      className="flex min-w-0 flex-col p-2"
    >
      {interaction.parts.map((part, index) => {
        const answer = submitted[part.part_id];
        return (
          <div
            key={part.part_id}
            data-part-id={part.part_id}
            className={`flex min-w-0 flex-col gap-2 px-1 py-2 ${index > 0 ? "border-t border-border" : ""}`}
          >
            <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed">
              {part.prompt}
            </p>
            {part.choices.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {part.choices.map((choice) => (
                  <span
                    key={choice.choice_id}
                    data-selected={answer?.choice_id === choice.choice_id}
                    className="flex max-w-full items-start gap-1.5 whitespace-pre-wrap break-words rounded-md border border-border px-2 py-1 text-xs data-[selected=true]:border-success data-[selected=true]:bg-accent"
                  >
                    {answer?.choice_id === choice.choice_id ? (
                      <CheckIcon className="size-3.5 shrink-0 text-success" />
                    ) : null}
                    <span className="min-w-0 break-words">{choice.text}</span>
                  </span>
                ))}
              </div>
            ) : null}
            {interaction.status === "resolved" ? (
              <p className="whitespace-pre-wrap break-words text-xs text-muted-foreground">
                {answer?.text ??
                  (answer?.choice_id
                    ? "Selected"
                    : part.choices.length > 0
                      ? "No choice selected"
                      : "No answer")}
              </p>
            ) : null}
          </div>
        );
      })}
      {interaction.response?.text != null ? (
        <p className="whitespace-pre-wrap break-words border-t border-border px-1 py-2 text-sm">
          {interaction.response.text}
        </p>
      ) : null}
    </div>
  );
}
