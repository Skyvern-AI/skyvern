import { CheckIcon, QuestionMarkCircledIcon } from "@radix-ui/react-icons";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import type { QuestionPart } from "../workflowCopilotTypes";
import { questionAnswerLine } from "./questionAnswerLine";

type QuestionPartsCardProps = {
  parts: QuestionPart[];
  // The message that followed this question, when one exists. Its lines are the
  // record of which parts were answered.
  answeredFrom: string | null;
  disabled: boolean;
  onAnswer: (message: string) => void;
};

export function QuestionPartsCard({
  parts,
  answeredFrom,
  disabled,
  onAnswer,
}: QuestionPartsCardProps) {
  const [selected, setSelected] = useState<Record<string, string>>({});
  const answeredLines = new Set(
    (answeredFrom ?? "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean),
  );
  const answerOf = (part: QuestionPart) =>
    part.choices.find((choice) =>
      answeredLines.has(questionAnswerLine(part.prompt, choice)),
    ) ?? null;
  const settled = parts.some((part) => answerOf(part) !== null);
  const pending = parts
    .map((part) => {
      const choice = selected[part.part_id];
      return choice ? questionAnswerLine(part.prompt, choice) : null;
    })
    .filter((line): line is string => line !== null);

  return (
    <div
      className="overflow-hidden rounded-lg border border-border bg-slate-elevation2"
      role="group"
      aria-label="Question parts"
    >
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <span className="flex size-6 flex-none items-center justify-center rounded-md bg-background">
          <QuestionMarkCircledIcon className="size-4 text-muted-foreground" />
        </span>
        <span className="text-xs font-semibold text-foreground">
          {parts.length === 1 ? "One thing to answer" : "Answer what you can"}
        </span>
      </div>
      <div className="flex flex-col p-2">
        {parts.map((part, index) => {
          const answered = answerOf(part);
          const active = answered ?? selected[part.part_id] ?? null;
          return (
            <div
              key={part.part_id}
              className={[
                "flex flex-col gap-1.5 px-1 py-2",
                index > 0 ? "border-t border-border" : "",
              ].join(" ")}
            >
              <span className="text-[13px] leading-[1.45] text-foreground">
                {part.prompt}
              </span>
              {part.choices.length === 0 ? (
                <span className="text-[11px] text-muted-foreground">
                  Type your answer in the message box.
                </span>
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {part.choices.map((choice) => {
                    const isActive = active === choice;
                    return (
                      <button
                        key={choice}
                        type="button"
                        disabled={disabled || settled}
                        aria-pressed={isActive}
                        onClick={() =>
                          setSelected((current) => ({
                            ...current,
                            [part.part_id]: choice,
                          }))
                        }
                        className={[
                          "flex items-center gap-1.5 rounded-md border px-2 py-1 text-left text-[12px] outline-none transition-colors",
                          "focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:cursor-default disabled:opacity-60",
                          isActive
                            ? "border-success bg-accent text-foreground"
                            : "border-border text-muted-foreground hover:bg-accent hover:text-foreground",
                        ].join(" ")}
                      >
                        {isActive ? (
                          <CheckIcon
                            className="size-3.5 flex-none text-success"
                            aria-hidden="true"
                          />
                        ) : null}
                        {choice}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
      {settled || disabled ? null : (
        <div className="flex items-center justify-between gap-2 border-t border-border px-3 py-2">
          <span className="text-[11px] text-muted-foreground">
            {pending.length} of {parts.length} answered
          </span>
          <Button
            size="sm"
            variant="secondary"
            disabled={pending.length === 0}
            onClick={() => onAnswer(pending.join("\n"))}
          >
            Send
          </Button>
        </div>
      )}
    </div>
  );
}
