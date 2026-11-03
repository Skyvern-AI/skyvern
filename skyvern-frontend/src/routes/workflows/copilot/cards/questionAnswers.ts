import type {
  QuestionAnswer,
  QuestionInteraction,
} from "../workflowCopilotTypes";

// The backend accepts an empty-string text as an answer, so only null means "not answered".
export function hasAnswer(answer: QuestionAnswer | undefined): boolean {
  return answer?.choice_id != null || answer?.text != null;
}

export function answeredPartIds(interaction: QuestionInteraction): Set<string> {
  return new Set(
    (interaction.response?.answers ?? [])
      .filter(hasAnswer)
      .map((answer) => answer.part_id),
  );
}
