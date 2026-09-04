import { CheckIcon, QuestionMarkCircledIcon } from "@radix-ui/react-icons";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { QuestionPart } from "../workflowCopilotTypes";
import {
  emissionPrompts,
  questionAnswerLabel,
  questionAnswerLine,
} from "./questionAnswerLine";

// Whitespace-insensitive comparison. The answer protocol is line-based, so a label carrying a
// newline would split across lines in the sent message and never match itself back. The server
// still admits one today (SKY-15617/15618 follow-up), so the card absorbs it here rather than
// dropping an answer the user actually gave.
const flatten = (value: string) => value.replace(/\s+/g, " ").trim();

type PartAnswer = { text: string; fromChoice: boolean };

// Which parts a previous message already answered, read off the card's OWN emitted label rather
// than off the user's words. Each label is claimed by at most one part, so two parts sharing a
// prompt cannot both take credit for one answer.
function resolveAnswers(
  parts: QuestionPart[],
  labels: string[],
  answeredFrom: string | null,
): Record<string, PartAnswer> {
  const message = answeredFrom ?? "";
  if (!message.trim()) return {};
  const flatMessage = flatten(message);
  const lines = message.split("\n");
  // Claim by line POSITION, not by text: two parts that share a prompt can be answered with the
  // same words, and a text-keyed claim would let the first one swallow both lines and leave its
  // twin looking unanswered.
  const takenLines = new Set<number>();
  const takenSpans = new Set<string>();
  const answers: Record<string, PartAnswer> = {};

  const takeLine = (matches: (flat: string) => boolean): number | null => {
    for (let index = 0; index < lines.length; index += 1) {
      if (takenLines.has(index)) continue;
      if (matches(flatten(lines[index] ?? ""))) {
        takenLines.add(index);
        return index;
      }
    }
    return null;
  };

  for (const [position, part] of parts.entries()) {
    // The emitted prompt first, then the plain one: a chat written before twins were
    // disambiguated still carries plain labels and must not orphan its receipts.
    const prompts = [labels[position] ?? part.prompt, part.prompt];
    const choiceOnItsOwnLine = part.choices.find((candidate) =>
      prompts.some(
        (prompt) =>
          takeLine(
            (flat) => flat === flatten(questionAnswerLine(prompt, candidate)),
          ) !== null,
      ),
    );
    if (choiceOnItsOwnLine !== undefined) {
      answers[part.part_id] = { text: choiceOnItsOwnLine, fromChoice: true };
      continue;
    }
    // A label that itself carries a newline spans lines, so it can only be found in the whole
    // message. Gated on the label actually containing one: without that gate this fallback
    // bypasses the positional claim above and lets a twin re-claim its sibling's answer.
    const spanning = part.choices.find((candidate) => {
      if (!questionAnswerLine(part.prompt, candidate).includes("\n"))
        return false;
      const label = flatten(questionAnswerLine(part.prompt, candidate));
      return !takenSpans.has(label) && flatMessage.includes(label);
    });
    if (spanning !== undefined) {
      takenSpans.add(flatten(questionAnswerLine(part.prompt, spanning)));
      answers[part.part_id] = { text: spanning, fromChoice: true };
      continue;
    }
    // Free-form receipt belongs only to a part that HAS no choices. A choiced part answered in
    // prose through the composer gets no per-part receipt — its bubble is the receipt — and
    // reading one here would settle the card off text it cannot show.
    if (part.choices.length > 0) continue;
    // The card emitted "<prompt> — ", so whatever follows that prefix on the first unclaimed
    // line carrying it is what the user sent for this part.
    for (const prompt of prompts) {
      const prefix = flatten(questionAnswerLabel(prompt));
      const index = takeLine(
        (flat) => flat.startsWith(prefix) && flat.length > prefix.length,
      );
      if (index !== null) {
        answers[part.part_id] = {
          text: flatten(lines[index] ?? "")
            .slice(prefix.length)
            .trim(),
          fromChoice: false,
        };
        break;
      }
    }
  }
  return answers;
}

// One choice-less part's answer slot: the recorded answer, or a field, or — on a read-only card —
// nothing at all. A read-only card renders no empty disabled box: that is exactly the affordance
// which cannot be used, which is the whole of SKY-15616. The prompt alone still says what was asked.
function FreeFormAnswer({
  inputId,
  answered,
  readOnly,
  value,
  onChange,
  onSubmit,
}: {
  inputId: string;
  answered: string | null;
  readOnly: boolean;
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
}) {
  if (answered !== null) {
    return (
      <span className="flex items-center gap-1.5 text-[13px] text-foreground">
        <CheckIcon
          className="size-3.5 flex-none text-success"
          aria-hidden="true"
        />
        {answered}
      </span>
    );
  }
  if (readOnly) return null;
  return (
    <Input
      id={inputId}
      // ponytail: the DS Input has no compact size, so the scale is matched by hand to the
      // chips beside it. The honest fix is a size="sm" variant.
      className="h-7 px-2 text-[12px]"
      value={value}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        // Enter while an IME is composing accepts the candidate; swallowing it here would
        // send a half-typed answer. Same guard the composer's handleKeyPress uses.
        if (event.nativeEvent.isComposing) return;
        if (event.key === "Enter") {
          event.preventDefault();
          onSubmit();
        }
      }}
    />
  );
}

type QuestionPartsCardProps = {
  parts: QuestionPart[];
  // The message that followed this question, when one exists. Its lines are the record of which
  // parts were answered. A user who answered in the composer instead matches nothing here, and
  // their own chat bubble is the receipt.
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
  const [typed, setTyped] = useState<Record<string, string>>({});
  const [picked, setPicked] = useState<Record<string, string>>({});
  const fieldId = useId();

  const labels = emissionPrompts(parts.map((part) => part.prompt));
  const answers = resolveAnswers(parts, labels, answeredFrom);
  const settled = Object.keys(answers).length > 0;
  const readOnly = disabled || settled;

  const pending = parts
    .map((part, position) => {
      const value = part.choices.length
        ? picked[part.part_id]
        : typed[part.part_id]?.trim();
      // Flatten the WHOLE line, not just the typed value. The prompt and the choices are
      // model-authored and this branch's admission still lets a newline through either, so any
      // of the three could otherwise split one answer into two protocol lines.
      return value
        ? flatten(questionAnswerLine(labels[position] ?? part.prompt, value))
        : null;
    })
    .filter((line): line is string => line !== null);

  const send = () => {
    if (pending.length > 0) onAnswer(pending.join("\n"));
  };

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
          const answered = answers[part.part_id];
          const active = answered?.text ?? picked[part.part_id] ?? null;
          const inputId = `${fieldId}-${part.part_id}`;
          const freeForm = part.choices.length === 0;
          return (
            <div
              key={part.part_id}
              className={[
                "flex flex-col gap-1.5 px-1 py-2",
                index > 0 ? "border-t border-border" : "",
              ].join(" ")}
            >
              <label
                htmlFor={freeForm ? inputId : undefined}
                className="text-[13px] leading-[1.45] text-foreground"
              >
                {part.prompt}
              </label>
              {freeForm ? (
                <FreeFormAnswer
                  inputId={inputId}
                  answered={answered?.text ?? null}
                  readOnly={readOnly}
                  value={typed[part.part_id] ?? ""}
                  onChange={(value) =>
                    setTyped((current) => ({
                      ...current,
                      [part.part_id]: value,
                    }))
                  }
                  onSubmit={send}
                />
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {part.choices.map((choice) => {
                    const isActive = active === choice;
                    return (
                      <button
                        key={choice}
                        type="button"
                        disabled={readOnly}
                        aria-pressed={isActive}
                        onClick={() =>
                          setPicked((current) => ({
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
      {readOnly ? null : (
        <div className="flex items-center justify-between gap-2 border-t border-border px-3 py-2">
          <span className="text-[11px] text-muted-foreground">
            {pending.length} of {parts.length} answered
          </span>
          <Button
            size="sm"
            variant="secondary"
            disabled={pending.length === 0}
            onClick={send}
          >
            Send
          </Button>
        </div>
      )}
    </div>
  );
}
