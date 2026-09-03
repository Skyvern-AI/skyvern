// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QuestionPartsCard } from "./QuestionPartsCard";
import { questionAnswerLine } from "./questionAnswerLine";
import { parseTerminalEnvelope } from "../narrativeState";

afterEach(() => {
  cleanup();
});

const parts = [
  { part_id: "p1", prompt: "Which store?", choices: ["Acme", "Borough"] },
  { part_id: "p2", prompt: "Which email gets the receipt?", choices: [] },
  { part_id: "p3", prompt: "Which day?", choices: ["Monday", "Friday"] },
];

describe("QuestionPartsCard", () => {
  it("sends only the parts the user actually answered", () => {
    const onAnswer = vi.fn();
    render(
      <QuestionPartsCard
        parts={parts}
        answeredFrom={null}
        disabled={false}
        onAnswer={onAnswer}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Borough" }));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(onAnswer).toHaveBeenCalledWith(
      questionAnswerLine("Which store?", "Borough"),
    );
    // A part whose answer is a value only the user holds still shows, so the
    // card never presents a compound ask as though it were a single question.
    expect(
      screen.getByText("Type your answer in the message box."),
    ).toBeTruthy();
  });

  it("reads the answered part back off the sent message after a reload", () => {
    render(
      <QuestionPartsCard
        parts={parts}
        answeredFrom={questionAnswerLine("Which day?", "Friday")}
        disabled
        onAnswer={vi.fn()}
      />,
    );

    expect(
      screen
        .getByRole("button", { name: "Friday" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(
      screen
        .getByRole("button", { name: "Monday" })
        .getAttribute("aria-pressed"),
    ).toBe("false");
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
  });
});

describe("parseTerminalEnvelope question parts", () => {
  it("keeps well-formed parts and drops the rest", () => {
    const facts = parseTerminalEnvelope({
      next_state: "awaiting_user_input",
      rendered_from_envelope: true,
      question_parts: [
        { part_id: "p1", prompt: "Which store?", choices: ["Acme", 7] },
        { prompt: "no id" },
        "junk",
      ],
    });

    expect(facts?.questionParts).toEqual([
      { part_id: "p1", prompt: "Which store?", choices: ["Acme"] },
    ]);
  });

  it("is empty on an envelope that carries no parts", () => {
    expect(
      parseTerminalEnvelope({ next_state: "stopped" })?.questionParts,
    ).toEqual([]);
  });
});
