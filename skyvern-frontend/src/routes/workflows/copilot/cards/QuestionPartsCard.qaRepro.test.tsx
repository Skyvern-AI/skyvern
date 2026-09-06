// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { QuestionInteraction } from "../workflowCopilotTypes";
import { QuestionPartsCard } from "./QuestionPartsCard";

afterEach(cleanup);

it("preserves question content without text, choice-count, or part-count vetoes", () => {
  const prompts = [
    "What should I send you?",
    "Which actions should this workflow never take?",
    "x".repeat(201),
    "Should ask_user explain the workflow run wr_fixture and session bs_fixture?",
    ...Array.from({ length: 5 }, (_, index) => `Preference ${index}`),
  ];
  const choices = [
    "Send me the receipt",
    "y".repeat(201),
    "No email",
    "Send an email",
    "Delete a file",
    "Explain execute_workflow",
    ...Array.from({ length: 3 }, (_, index) => `Choice ${index}`),
  ];
  const interaction: QuestionInteraction = {
    interaction_id: "i",
    turn_id: "t",
    tool_call_id: "c",
    status: "pending",
    response: null,
    created_at: "2026-09-04T00:00:00Z",
    resolved_at: null,
    parts: prompts.map((prompt, index) => ({
      part_id: `part-${index}`,
      prompt,
      choices: choices.map((text, choice) => ({
        choice_id: `choice-${index}-${choice}`,
        text,
      })),
    })),
  };
  const onAnswer = vi.fn();
  render(<QuestionPartsCard interaction={interaction} onAnswer={onAnswer} />);
  expect(screen.getAllByRole("textbox")).toHaveLength(1);
  for (const prompt of prompts) expect(screen.getByText(prompt)).toBeTruthy();
  for (const choice of choices)
    expect(screen.getAllByRole("button", { name: choice })).toHaveLength(9);
  fireEvent.click(screen.getAllByRole("button", { name: choices[8] })[8]!);
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(onAnswer).toHaveBeenCalledWith({
    answers: [{ part_id: "part-8", choice_id: "choice-8-8" }],
  });
});

it("answers only the second of identical prompts by identity", () => {
  const interaction: QuestionInteraction = {
    interaction_id: "i",
    turn_id: "t",
    tool_call_id: "c",
    status: "pending",
    response: null,
    created_at: "2026-09-04T00:00:00Z",
    resolved_at: null,
    parts: [
      {
        part_id: "first",
        prompt: "Which format?",
        choices: [{ choice_id: "mon", text: "PDF" }],
      },
      {
        part_id: "second",
        prompt: "Which format?",
        choices: [{ choice_id: "fri", text: "CSV" }],
      },
    ],
  };
  const onAnswer = vi.fn();
  const { container, rerender } = render(
    <QuestionPartsCard interaction={interaction} onAnswer={onAnswer} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "CSV" }));
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  const response = { answers: [{ part_id: "second", choice_id: "fri" }] };
  expect(onAnswer).toHaveBeenCalledWith(response);
  rerender(
    <QuestionPartsCard
      interaction={{ ...interaction, status: "resolved", response }}
      onAnswer={onAnswer}
    />,
  );
  expect(
    within(
      container.querySelector('[data-part-id="first"]')! as HTMLElement,
    ).getByText("No choice selected"),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "CSV" }).getAttribute("aria-pressed"),
  ).toBe("true");
});
