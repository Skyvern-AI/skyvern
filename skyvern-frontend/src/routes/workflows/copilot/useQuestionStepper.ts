import { useCallback, useState } from "react";

import type {
  QuestionAnswer,
  QuestionInteraction,
  QuestionResponse,
} from "./workflowCopilotTypes";

type StepperState = {
  interactionId: string | null;
  index: number;
  choices: Record<string, string>;
  // Text for parts the user has stepped away from. The part on screen keeps its text in the
  // composer, so the composer stays the one free-text field.
  texts: Record<string, string>;
};

const EMPTY: StepperState = {
  interactionId: null,
  index: 0,
  choices: {},
  texts: {},
};

export type QuestionStepper = {
  index: number;
  isLast: boolean;
  choices: Record<string, string>;
  answeredCount: number;
  toggleChoice: (partId: string, choiceId: string) => void;
  goTo: (index: number) => void;
  // Leaves the part on screen unanswered and moves to the next one.
  skipCurrent: () => void;
  // Without the part on screen when the user skips it on the last step. No answers at all is
  // an explicit skip of the whole question.
  buildResponse: (options?: { omitCurrent?: boolean }) => QuestionResponse;
};

export function useQuestionStepper(
  interaction: QuestionInteraction | undefined,
  draft: string,
  setDraft: (value: string) => void,
): QuestionStepper {
  const [stored, setStored] = useState<StepperState>(EMPTY);
  // The history poll replaces interaction objects every few seconds, so progress is keyed by
  // id rather than identity.
  const interactionId = interaction?.interaction_id ?? null;
  const state =
    stored.interactionId === interactionId
      ? stored
      : { ...EMPTY, interactionId };
  const parts = interaction?.parts ?? [];
  const index = Math.min(state.index, Math.max(parts.length - 1, 0));
  const currentPartId = parts[index]?.part_id;

  const textFor = (partId: string) =>
    partId === currentPartId ? draft : (state.texts[partId] ?? "");

  const answersFor = (omitPartId?: string): QuestionAnswer[] =>
    parts.flatMap((part) => {
      if (part.part_id === omitPartId) return [];
      const choiceId = state.choices[part.part_id];
      const text = textFor(part.part_id);
      if (!choiceId && text === "") return [];
      return [
        {
          part_id: part.part_id,
          ...(choiceId ? { choice_id: choiceId } : {}),
          ...(text !== "" ? { text } : {}),
        },
      ];
    });
  const answers = answersFor();

  const toggleChoice = useCallback(
    (partId: string, choiceId: string) => {
      setStored((previous) => {
        const base =
          previous.interactionId === interactionId
            ? previous
            : { ...EMPTY, interactionId };
        const choices = { ...base.choices };
        if (choices[partId] === choiceId) delete choices[partId];
        else choices[partId] = choiceId;
        return { ...base, choices };
      });
    },
    [interactionId],
  );

  const goTo = (next: number) => {
    if (!currentPartId || next < 0 || next >= parts.length || next === index)
      return;
    const nextPartId = parts[next]!.part_id;
    setStored({
      ...state,
      index: next,
      texts: { ...state.texts, [currentPartId]: draft },
    });
    setDraft(state.texts[nextPartId] ?? "");
  };

  const skipCurrent = () => {
    if (!currentPartId || index >= parts.length - 1) return;
    const nextPartId = parts[index + 1]!.part_id;
    const choices = { ...state.choices };
    delete choices[currentPartId];
    setStored({
      ...state,
      index: index + 1,
      choices,
      texts: { ...state.texts, [currentPartId]: "" },
    });
    setDraft(state.texts[nextPartId] ?? "");
  };

  return {
    index,
    isLast: index >= parts.length - 1,
    choices: state.choices,
    answeredCount: answers.length,
    toggleChoice,
    goTo,
    skipCurrent,
    buildResponse: (options) => {
      const sent = options?.omitCurrent ? answersFor(currentPartId) : answers;
      return sent.length > 0 ? { answers: sent } : { skipped: true };
    },
  };
}
