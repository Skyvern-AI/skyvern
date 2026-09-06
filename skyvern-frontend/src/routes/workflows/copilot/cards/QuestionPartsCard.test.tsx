// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { QuestionInteraction } from "../workflowCopilotTypes";
import { QuestionPartsCard } from "./QuestionPartsCard";

afterEach(cleanup);

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
      part_id: "day",
      prompt: "Which weekday?",
      choices: [{ choice_id: "tue", text: "Tuesday" }],
    },
    {
      part_id: "delivery",
      prompt: "Email delivery?",
      choices: [{ choice_id: "no", text: "No email" }],
    },
  ],
};

describe("durable questions", () => {
  it("submits partial answers with stable IDs and no synthesized prose", () => {
    const onAnswer = vi.fn();
    render(<QuestionPartsCard interaction={pending} onAnswer={onAnswer} />);
    fireEvent.click(screen.getByRole("button", { name: "Tuesday" }));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onAnswer).toHaveBeenCalledWith({
      answers: [{ part_id: "day", choice_id: "tue" }],
    });
  });

  it("offers verbatim free text even when choices exist", () => {
    const onAnswer = vi.fn();
    render(<QuestionPartsCard interaction={pending} onAnswer={onAnswer} />);
    fireEvent.change(screen.getByRole("textbox", { name: "Your response" }), {
      target: { value: "why do you need this?\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onAnswer).toHaveBeenCalledWith({
      answers: [],
      text: "why do you need this?\n",
    });
  });

  it("skips explicitly without inventing an answer", () => {
    const onAnswer = vi.fn();
    render(<QuestionPartsCard interaction={pending} onAnswer={onAnswer} />);
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    expect(onAnswer).toHaveBeenCalledWith({ skipped: true });
  });

  it("hydrates receipts from persisted IDs after remounting", () => {
    const resolved: QuestionInteraction = {
      ...pending,
      status: "resolved",
      response: { answers: [{ part_id: "delivery", choice_id: "no" }] },
    };
    const onAnswer = vi.fn();
    const first = render(
      <QuestionPartsCard interaction={resolved} onAnswer={onAnswer} />,
    );
    first.unmount();
    render(
      <QuestionPartsCard
        interaction={JSON.parse(JSON.stringify(resolved))}
        onAnswer={onAnswer}
      />,
    );
    expect(
      screen
        .getByRole("button", { name: "No email" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(
      screen
        .getByRole("button", { name: "Tuesday" })
        .getAttribute("aria-pressed"),
    ).toBe("false");
    expect(screen.getByText("No choice selected")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Tuesday" }));
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("keeps cancelled questions read-only", () => {
    render(
      <QuestionPartsCard
        interaction={{ ...pending, status: "cancelled" }}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByText("Question cancelled")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});

it("keeps a selected choice and contradictory bottom-field text together", () => {
  const onAnswer = vi.fn();
  render(
    <QuestionPartsCard
      interaction={{
        ...pending,
        parts: [
          {
            part_id: "delivery",
            prompt: "How should I deliver it?",
            choices: [{ choice_id: "email", text: "Email" }],
          },
        ],
      }}
      onAnswer={onAnswer}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Email" }));
  expect(onAnswer).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("textbox", { name: "Your response" }), {
    target: { value: "Do not email it.\nLet me download it instead." },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(onAnswer).toHaveBeenCalledWith({
    answers: [{ part_id: "delivery", choice_id: "email" }],
    text: "Do not email it.\nLet me download it instead.",
  });
});

it("shows interrupted questions without submission controls", () => {
  render(
    <QuestionPartsCard
      interaction={{ ...pending, status: "interrupted" }}
      onAnswer={vi.fn()}
    />,
  );
  expect(screen.getByText("Question interrupted")).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
});
