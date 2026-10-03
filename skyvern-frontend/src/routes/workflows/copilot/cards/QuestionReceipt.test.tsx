// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { QuestionInteraction } from "../workflowCopilotTypes";
import { QuestionReceipt } from "./QuestionReceipt";

afterEach(cleanup);

const asked: QuestionInteraction = {
  interaction_id: "interaction",
  turn_id: "turn",
  tool_call_id: "call",
  status: "pending",
  response: null,
  created_at: "2026-09-04T00:00:00Z",
  resolved_at: null,
  parts: [
    {
      part_id: "first",
      prompt: "Which format?",
      choices: [{ choice_id: "pdf", text: "PDF" }],
    },
    {
      part_id: "second",
      prompt: "Which format?",
      choices: [{ choice_id: "csv", text: "CSV" }],
    },
  ],
};

it("points at the composer while pending instead of offering controls", () => {
  render(<QuestionReceipt interaction={asked} />);
  expect(screen.getByText("Copilot asked 2 questions")).toBeTruthy();
  expect(screen.queryByRole("button")).toBeNull();
  expect(screen.queryByText("Which format?")).toBeNull();
});

it("hydrates the record from persisted IDs after remounting", () => {
  const resolved: QuestionInteraction = {
    ...asked,
    parts: [
      ...asked.parts,
      { part_id: "third", prompt: "Anything else?", choices: [] },
    ],
    status: "resolved",
    response: {
      answers: [{ part_id: "second", choice_id: "csv", text: "Zip it" }],
    },
  };
  const first = render(<QuestionReceipt interaction={resolved} />);
  first.unmount();
  const { container } = render(
    <QuestionReceipt interaction={JSON.parse(JSON.stringify(resolved))} />,
  );
  expect(screen.getByText("You answered 1 of 3 questions")).toBeTruthy();
  expect(screen.queryByText("CSV")).toBeNull();
  fireEvent.click(
    screen.getByRole("button", { name: /You answered 1 of 3 questions/ }),
  );
  const part = (id: string) =>
    within(container.querySelector(`[data-part-id="${id}"]`) as HTMLElement);
  expect(part("first").getByText("Skipped")).toBeTruthy();
  expect(part("third").getByText("Skipped")).toBeTruthy();
  expect(
    part("second")
      .getByText("CSV")
      .closest("[data-selected]")
      ?.getAttribute("data-selected"),
  ).toBe("true");
  expect(part("second").getByText("Zip it")).toBeTruthy();
  expect(part("second").getByText("Selected:")).toBeTruthy();
});

it("counts an empty-string answer as answered, as the backend does", () => {
  render(
    <QuestionReceipt
      interaction={{
        ...asked,
        status: "resolved",
        response: {
          answers: [
            { part_id: "first", choice_id: "pdf" },
            { part_id: "second", choice_id: null, text: "" },
          ],
        },
      }}
    />,
  );
  expect(screen.getByText("You answered 2 questions")).toBeTruthy();
  fireEvent.click(
    screen.getByRole("button", { name: /You answered 2 questions/ }),
  );
  expect(screen.getByText("Sent with no text")).toBeTruthy();
  expect(screen.queryByText("Skipped")).toBeNull();
});

it("keeps skipped, cancelled, and interrupted questions read-only", () => {
  const { rerender } = render(
    <QuestionReceipt
      interaction={{
        ...asked,
        status: "resolved",
        response: { skipped: true },
      }}
    />,
  );
  expect(screen.getByText("You skipped 2 questions")).toBeTruthy();
  rerender(<QuestionReceipt interaction={{ ...asked, status: "cancelled" }} />);
  expect(screen.getByText("Question cancelled")).toBeTruthy();
  rerender(
    <QuestionReceipt interaction={{ ...asked, status: "interrupted" }} />,
  );
  expect(screen.getByText("Question interrupted")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /Question interrupted/ }));
  expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
  expect(screen.queryByRole("textbox")).toBeNull();
});

it("shows a whole-question reply without marking each part skipped", () => {
  render(
    <QuestionReceipt
      interaction={{
        ...asked,
        status: "resolved",
        response: { text: "Use whichever is smaller" },
      }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /You replied/ }));
  expect(screen.getByText("Use whichever is smaller")).toBeTruthy();
  expect(screen.queryByText("Skipped")).toBeNull();
});
