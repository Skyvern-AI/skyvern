// @vitest-environment jsdom
//
// Pins the four user-facing guarantees of the question card: it never offers a Send it cannot
// enable, its counter can always reach its own denominator, an answer is attributed to the part
// that was answered rather than to a twin, and a multi-line choice stays legible once answered.
// Each began as a QA repro that failed on the shipped build; adopted into #16625 with the fixes.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QuestionPartsCard } from "./QuestionPartsCard";
import { questionAnswerLine } from "./questionAnswerLine";

afterEach(() => {
  cleanup();
});

describe("QuestionPartsCard — QA repros", () => {
  // REPRO 1 (MAJOR): every part is choice-less, so `pending` can never be
  // non-empty and the Send button can never enable. The card still renders
  // "0 of N answered" next to a permanently dead Send, and carries no input of
  // its own, so the affordance reads as broken input.
  // Observed live on staging: chat wcc_570372112386389568, turn 2.
  it("does not offer a Send affordance it can never enable", () => {
    const onAnswer = vi.fn();
    render(
      <QuestionPartsCard
        parts={[
          { part_id: "p1", prompt: "Which catalog page?", choices: [] },
          { part_id: "p2", prompt: "Which email?", choices: [] },
        ]}
        answeredFrom={null}
        disabled={false}
        onAnswer={onAnswer}
      />,
    );

    const send = screen.queryByRole("button", { name: "Send" });
    const card = screen.getByRole("group", { name: "Question parts" });
    const canAnswerInCard =
      card.querySelectorAll("input, textarea").length > 0 ||
      screen.queryAllByRole("button", { pressed: false }).length > 0;

    // Either the card offers a way to answer in place, or it must not render a
    // counter and Send button that nothing the user does can ever satisfy.
    if (send !== null) {
      expect(canAnswerInCard).toBe(true);
    }
  });

  // REPRO 2 (MAJOR): the mixed case. The counter's denominator is every part,
  // but only a part with choices can ever become pending, so the count can
  // never reach its own denominator. Observed live on staging: chat
  // wcc_570372112386389568 turn 1 (p1 free-form, p2 with three choices) tops
  // out at "1 of 2 answered".
  it("does not show a counter whose denominator it can never reach", () => {
    render(
      <QuestionPartsCard
        parts={[
          { part_id: "p1", prompt: "Which catalog page?", choices: [] },
          {
            part_id: "p2",
            prompt: "Which fields?",
            choices: ["Name", "Price"],
          },
        ]}
        answeredFrom={null}
        disabled={false}
        onAnswer={vi.fn()}
      />,
    );

    // Answer everything the card lets the user answer.
    fireEvent.change(screen.getByLabelText("Which catalog page?"), {
      target: { value: "the catalog page" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Name" }));

    const card = screen.getByRole("group", { name: "Question parts" });
    const counter = card.textContent?.match(/(\d+) of (\d+) answered/);
    expect(counter).not.toBeNull();
    expect(counter![1]).toBe(counter![2]);
  });

  // REPRO 2 (MINOR): answer attribution is keyed on prompt TEXT, not on the
  // server-minted part_id. Two parts whose prompts collide are both marked
  // answered when only one was, and `settled` then freezes the whole card.
  it("attributes an answer to the part that was answered, not to its twin", () => {
    render(
      <QuestionPartsCard
        parts={[
          { part_id: "p1", prompt: "Which day?", choices: ["Monday"] },
          {
            part_id: "p2",
            prompt: "Which day?",
            choices: ["Monday", "Friday"],
          },
        ]}
        answeredFrom={questionAnswerLine("Which day?", "Monday")}
        disabled
        onAnswer={vi.fn()}
      />,
    );

    const pressed = screen
      .getAllByRole("button")
      .filter((b) => b.getAttribute("aria-pressed") === "true");
    expect(pressed).toHaveLength(1);
  });

  // REPRO 3 (MINOR): a choice label carrying a newline is admitted by the
  // server but breaks the client's line-based answer protocol: the sent
  // message splits across lines, so the answered state can never be read back.
  it("keeps a multi-line choice legible after it is answered", () => {
    const choice = "Line one\nLine two";
    render(
      <QuestionPartsCard
        parts={[{ part_id: "p1", prompt: "Pick", choices: [choice] }]}
        answeredFrom={questionAnswerLine("Pick", choice)}
        disabled
        onAnswer={vi.fn()}
      />,
    );

    expect(
      screen
        .getByRole("button", { name: /Line one/ })
        .getAttribute("aria-pressed"),
    ).toBe("true");
  });
});
