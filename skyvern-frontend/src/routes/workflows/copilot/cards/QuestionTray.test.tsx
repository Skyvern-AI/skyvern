// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { useQuestionStepper } from "../useQuestionStepper";
import type {
  QuestionInteraction,
  QuestionResponse,
} from "../workflowCopilotTypes";
import { QuestionTray } from "./QuestionTray";

afterEach(cleanup);

// The chat's wiring in miniature: the composer holds the text for the part on screen.
function Composer({
  interaction,
  onAnswer,
}: {
  interaction: QuestionInteraction;
  onAnswer: (response: QuestionResponse) => void;
}) {
  const [draft, setDraft] = useState("");
  const stepper = useQuestionStepper(interaction, draft, setDraft);
  return (
    <>
      <QuestionTray
        interaction={interaction}
        stepper={stepper}
        disabled={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onSend={() => onAnswer(stepper.buildResponse())}
        onSkip={() => onAnswer(stepper.buildResponse({ omitCurrent: true }))}
      />
      <textarea
        aria-label="Your response"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
      />
    </>
  );
}

const pending: QuestionInteraction = {
  interaction_id: "interaction",
  turn_id: "turn",
  tool_call_id: "call",
  status: "pending",
  response: null,
  created_at: "2026-09-04T00:00:00Z",
  resolved_at: null,
  parts: [
    {
      part_id: "members",
      prompt: "How many members does the LLC have?",
      choices: [
        { choice_id: "one", text: "1" },
        { choice_id: "two", text: "2" },
      ],
    },
    {
      part_id: "state",
      prompt: "Which state was the LLC formed in?",
      choices: [],
    },
  ],
};

const composer = () => screen.getByRole("textbox", { name: "Your response" });

it("steps one question at a time and sends every part's answer by identity", () => {
  const onAnswer = vi.fn();
  render(<Composer interaction={pending} onAnswer={onAnswer} />);
  expect(screen.queryByText(pending.parts[1]!.prompt)).toBeNull();
  expect(screen.queryByRole("button", { name: "Send" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "1" }));
  fireEvent.change(composer(), { target: { value: "Single-member LLC" } });
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(screen.getByText(pending.parts[1]!.prompt)).toBeTruthy();
  expect((composer() as HTMLTextAreaElement).value).toBe("");

  fireEvent.change(composer(), { target: { value: "Delaware" } });
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(
    screen.getByRole("button", { name: "1" }).getAttribute("aria-pressed"),
  ).toBe("true");
  expect((composer() as HTMLTextAreaElement).value).toBe("Single-member LLC");

  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  expect((composer() as HTMLTextAreaElement).value).toBe("Delaware");
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(onAnswer).toHaveBeenCalledWith({
    answers: [
      { part_id: "members", choice_id: "one", text: "Single-member LLC" },
      { part_id: "state", text: "Delaware" },
    ],
  });
});

it("sends a partial answer, and nothing until something is answered", () => {
  const onAnswer = vi.fn();
  render(<Composer interaction={pending} onAnswer={onAnswer} />);
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  const send = screen.getByRole("button", { name: "Send" });
  expect(send.matches(":disabled")).toBe(true);
  fireEvent.change(composer(), { target: { value: "Do not file yet.\n" } });
  fireEvent.click(send);
  expect(onAnswer).toHaveBeenCalledWith({
    answers: [{ part_id: "state", text: "Do not file yet.\n" }],
  });
});

it("skips only the question on screen, and the whole card when nothing was answered", () => {
  const onAnswer = vi.fn();
  const { unmount } = render(
    <Composer interaction={pending} onAnswer={onAnswer} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "1" }));
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  fireEvent.change(composer(), { target: { value: "Delaware" } });
  fireEvent.click(screen.getByRole("button", { name: "Skip" }));
  expect(onAnswer).toHaveBeenLastCalledWith({
    answers: [{ part_id: "members", choice_id: "one" }],
  });
  unmount();

  render(<Composer interaction={pending} onAnswer={onAnswer} />);
  fireEvent.click(screen.getByRole("button", { name: "1" }));
  fireEvent.change(composer(), { target: { value: "Single member" } });
  fireEvent.click(screen.getByRole("button", { name: "Skip" }));
  expect(screen.getByText(pending.parts[1]!.prompt)).toBeTruthy();
  expect((composer() as HTMLTextAreaElement).value).toBe("");
  fireEvent.click(screen.getByRole("button", { name: "Skip" }));
  expect(onAnswer).toHaveBeenLastCalledWith({ skipped: true });
});

it("keeps keyboard focus in the tray when Back returns to the first question", () => {
  render(<Composer interaction={pending} onAnswer={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(document.activeElement).toBe(
    screen.getByRole("button", { name: "Next" }),
  );
  fireEvent.keyDown(document.activeElement!, { key: "2" });
  expect(
    screen.getByRole("button", { name: "2" }).getAttribute("aria-pressed"),
  ).toBe("true");
});

it("reaches every part and choice without text, choice-count, or part-count vetoes", () => {
  const prompts = [
    "What should I send you?",
    "x".repeat(201),
    "Should ask_user explain the workflow run wr_fixture and session bs_fixture?",
    ...Array.from({ length: 5 }, () => "Which format?"),
    "Which format?",
  ];
  const choices = [
    "Send me the receipt",
    "y".repeat(201),
    "Explain execute_workflow",
    ...Array.from({ length: 6 }, (_, index) => `Choice ${index}`),
  ];
  const interaction: QuestionInteraction = {
    ...pending,
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
  render(<Composer interaction={interaction} onAnswer={onAnswer} />);
  for (const [index, prompt] of prompts.entries()) {
    expect(screen.getByText(prompt)).toBeTruthy();
    for (const choice of choices)
      expect(screen.getByText(choice).closest("button")).toBeTruthy();
    if (index < prompts.length - 1)
      fireEvent.click(screen.getByRole("button", { name: "Next" }));
  }
  // The last two parts share their wording; only the one on screen is answered.
  fireEvent.click(screen.getByRole("button", { name: choices[8] }));
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(onAnswer).toHaveBeenCalledWith({
    answers: [{ part_id: "part-8", choice_id: "choice-8-8" }],
  });
});
