// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QuestionPartsCard } from "./QuestionPartsCard";
import { questionAnswerLine } from "./questionAnswerLine";
import { parseTerminalEnvelope } from "../narrativeState";

afterEach(() => {
  cleanup();
});

const mixed = [
  { part_id: "p1", prompt: "Which store?", choices: ["Acme", "Borough"] },
  { part_id: "p2", prompt: "Which email gets the receipt?", choices: [] },
  { part_id: "p3", prompt: "Which day?", choices: ["Monday", "Friday"] },
];

// 86% of live asks. Never constructed in a test before SKY-15619, which is why SKY-15616 shipped.
const allChoiceless = [
  { part_id: "p1", prompt: "Which store?", choices: [] },
  { part_id: "p2", prompt: "Which email gets the receipt?", choices: [] },
];

const counterOf = () =>
  screen
    .getByRole("group", { name: "Question parts" })
    .textContent?.match(/(\d+) of (\d+) answered/);

const fieldFor = (prompt: string) =>
  screen.getByLabelText(prompt) as HTMLInputElement;

describe("QuestionPartsCard", () => {
  it("sends only the parts the user actually answered", () => {
    const onAnswer = vi.fn();
    render(
      <QuestionPartsCard
        parts={mixed}
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
  });

  it("gives every choice-less part a field of its own", () => {
    render(
      <QuestionPartsCard
        parts={allChoiceless}
        answeredFrom={null}
        disabled={false}
        onAnswer={vi.fn()}
      />,
    );

    expect(fieldFor("Which store?")).toBeTruthy();
    expect(fieldFor("Which email gets the receipt?")).toBeTruthy();
  });

  // The counter has to be reachable in every shape the model actually produces, or it is telling
  // the user about work they cannot do. Pins the second half of SKY-15616.
  it.each([
    ["all choice-less", allChoiceless],
    ["mixed", mixed],
    ["single", [{ part_id: "p1", prompt: "Which store?", choices: [] }]],
  ])("lets the counter reach its denominator: %s", (_name, parts) => {
    render(
      <QuestionPartsCard
        parts={parts}
        answeredFrom={null}
        disabled={false}
        onAnswer={vi.fn()}
      />,
    );

    for (const part of parts) {
      if (part.choices.length) {
        fireEvent.click(
          screen.getByRole("button", { name: part.choices[0] as string }),
        );
      } else {
        fireEvent.change(fieldFor(part.prompt), {
          target: { value: "an answer" },
        });
      }
    }

    const counter = counterOf();
    expect(counter).not.toBeNull();
    expect(counter![1]).toBe(String(parts.length));
    expect(counter![1]).toBe(counter![2]);
  });

  it("sends what is filled when Enter is pressed in a field", () => {
    const onAnswer = vi.fn();
    render(
      <QuestionPartsCard
        parts={allChoiceless}
        answeredFrom={null}
        disabled={false}
        onAnswer={onAnswer}
      />,
    );

    fireEvent.change(fieldFor("Which store?"), {
      target: { value: "acme.example" },
    });
    fireEvent.keyDown(fieldFor("Which store?"), { key: "Enter" });

    expect(onAnswer).toHaveBeenCalledWith(
      questionAnswerLine("Which store?", "acme.example"),
    );
  });

  it("reads a free-form answer back off the label the card itself emitted", () => {
    render(
      <QuestionPartsCard
        parts={allChoiceless}
        answeredFrom={questionAnswerLine("Which store?", "acme.example")}
        disabled
        onAnswer={vi.fn()}
      />,
    );

    expect(screen.getByText("acme.example")).toBeTruthy();
    // The unanswered part keeps its prompt but no longer offers a live field.
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
  });

  it("shows no per-part receipt when the answer came through the composer", () => {
    render(
      <QuestionPartsCard
        parts={allChoiceless}
        answeredFrom="it's acme, and send it to me"
        disabled
        onAnswer={vi.fn()}
      />,
    );

    // Nothing matched the card's own labels, so the chat bubble is the receipt and the card is
    // read-only rather than claiming an answer it cannot attribute.
    expect(screen.queryByText("it's acme, and send it to me")).toBeNull();
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("reads the answered part back off the sent message after a reload", () => {
    render(
      <QuestionPartsCard
        parts={mixed}
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

  // QA's twin+gap probe: two parts share a prompt and the user fills only the SECOND. One line
  // is emitted, and a claim-once-in-order receipt would hand it to the first twin — marking a
  // part the user never answered and blanking the one they did. Twins are rare; a blank field
  // beside a filled one is ordinary under D2, so the gap half is common.
  it("lands a twin's receipt on the part the user actually filled", () => {
    const twins = [
      { part_id: "p1", prompt: "Which day?", choices: [] },
      { part_id: "p2", prompt: "Which day?", choices: [] },
    ];
    const onAnswer = vi.fn();
    const live = render(
      <QuestionPartsCard
        parts={twins}
        answeredFrom={null}
        disabled={false}
        onAnswer={onAnswer}
      />,
    );

    // Only the second twin is filled.
    fireEvent.change(screen.getAllByRole("textbox")[1] as HTMLInputElement, {
      target: { value: "Friday" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const sent = onAnswer.mock.calls[0]![0] as string;
    live.unmount();

    render(
      <QuestionPartsCard
        parts={twins}
        answeredFrom={sent}
        disabled
        onAnswer={vi.fn()}
      />,
    );

    const rows = screen
      .getByRole("group", { name: "Question parts" })
      .querySelectorAll(":scope > div > div");
    expect(rows[0]?.textContent).not.toContain("Friday");
    expect(rows[1]?.textContent).toContain("Friday");
  });

  // Under D2 the user types the transport text, so a multi-line paste must not put a newline
  // into the emitted line — one answer would split across two and be unreadable on reload.
  it("keeps a multi-line paste on one emitted line, and reads it back", () => {
    const parts = [{ part_id: "p1", prompt: "Which store?", choices: [] }];
    const onAnswer = vi.fn();
    const live = render(
      <QuestionPartsCard
        parts={parts}
        answeredFrom={null}
        disabled={false}
        onAnswer={onAnswer}
      />,
    );

    fireEvent.change(fieldFor("Which store?"), {
      target: { value: "acme.example\nsecond line" },
    });
    // The single-line Input drops the newline on the way in, which is the browser behaviour this
    // pins rather than assumes. Whatever it kept is what must round-trip.
    const held = fieldFor("Which store?").value;
    expect(held).not.toContain("\n");
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    const sent = onAnswer.mock.calls[0]![0] as string;
    expect(sent).not.toContain("\n");
    expect(sent).toBe(`Which store? — ${held}`);
    live.unmount();

    render(
      <QuestionPartsCard
        parts={parts}
        answeredFrom={sent}
        disabled
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByText(held)).toBeTruthy();
  });

  // Backward compatibility: a chat written before twins were disambiguated carries the plain
  // "<prompt> — <answer>" format. Those receipts must still resolve, or every stored chat
  // silently loses its answered state on the deploy that ships the suffix.
  it("still reads back a receipt written in the pre-suffix plain format", () => {
    render(
      <QuestionPartsCard
        parts={[{ part_id: "p1", prompt: "Which store?", choices: [] }]}
        answeredFrom="Which store? — acme.example"
        disabled
        onAnswer={vi.fn()}
      />,
    );

    expect(screen.getByText("acme.example")).toBeTruthy();
  });

  // Enter while an IME is composing commits the candidate; sending there would ship a half-typed
  // answer the user never confirmed.
  it("does not send on the Enter that commits an IME candidate", () => {
    const onAnswer = vi.fn();
    render(
      <QuestionPartsCard
        parts={allChoiceless}
        answeredFrom={null}
        disabled={false}
        onAnswer={onAnswer}
      />,
    );

    fireEvent.change(fieldFor("Which store?"), { target: { value: "アク" } });
    fireEvent.keyDown(fieldFor("Which store?"), {
      key: "Enter",
      isComposing: true,
    });
    expect(onAnswer).not.toHaveBeenCalled();

    // The Enter that follows the commit is a real send.
    fireEvent.keyDown(fieldFor("Which store?"), { key: "Enter" });
    expect(onAnswer).toHaveBeenCalledTimes(1);
  });

  // The prompt and the choices are model-authored, and this branch's admission still lets a
  // newline through either, so the whole emitted line is normalised — not just the typed value.
  // Round-tripped through a real click/send rather than a prebuilt answeredFrom string.
  it.each([
    [
      "a multi-line prompt",
      [{ part_id: "p1", prompt: "Which\nstore?", choices: [] }],
      "acme.example",
    ],
    [
      "a multi-line choice",
      [
        {
          part_id: "p1",
          prompt: "Which store?",
          choices: ["Acme\nSupply", "Borough"],
        },
      ],
      "Acme\nSupply",
    ],
  ])(
    "emits one protocol line for %s, and reads it back",
    (_name, parts, answer) => {
      const onAnswer = vi.fn();
      const choiced = parts[0]!.choices.length > 0;
      const live = render(
        <QuestionPartsCard
          parts={parts}
          answeredFrom={null}
          disabled={false}
          onAnswer={onAnswer}
        />,
      );

      if (choiced) {
        fireEvent.click(screen.getByRole("button", { name: /Acme/ }));
      } else {
        fireEvent.change(screen.getByRole("textbox"), {
          target: { value: answer },
        });
      }
      fireEvent.click(screen.getByRole("button", { name: "Send" }));

      const sent = onAnswer.mock.calls[0]![0] as string;
      expect(sent).not.toContain("\n");
      live.unmount();

      render(
        <QuestionPartsCard
          parts={parts}
          answeredFrom={sent}
          disabled
          onAnswer={vi.fn()}
        />,
      );

      // The part reads back as answered rather than reverting to unanswered.
      if (choiced) {
        expect(
          screen
            .getByRole("button", { name: /Acme/ })
            .getAttribute("aria-pressed"),
        ).toBe("true");
      } else {
        expect(screen.getByText(answer)).toBeTruthy();
        expect(screen.queryByRole("textbox")).toBeNull();
      }
    },
  );

  // The emitted line is whitespace-normalised, and that is lossy: these two prompts are different
  // raw strings that would otherwise persist the same prefix, so answering the second would reload
  // onto the first. Round-tripped through a real send.
  it("keeps prompts that differ only by whitespace on their own rows after reload", () => {
    const parts = [
      { part_id: "p1", prompt: "Which\nstore?", choices: [] },
      { part_id: "p2", prompt: "Which store?", choices: [] },
    ];
    const onAnswer = vi.fn();
    const live = render(
      <QuestionPartsCard
        parts={parts}
        answeredFrom={null}
        disabled={false}
        onAnswer={onAnswer}
      />,
    );

    // Only the SECOND is filled.
    fireEvent.change(screen.getAllByRole("textbox")[1] as HTMLInputElement, {
      target: { value: "acme.example" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const sent = onAnswer.mock.calls[0]![0] as string;
    live.unmount();

    render(
      <QuestionPartsCard
        parts={parts}
        answeredFrom={sent}
        disabled
        onAnswer={vi.fn()}
      />,
    );

    const rows = screen
      .getByRole("group", { name: "Question parts" })
      .querySelectorAll(":scope > div > div");
    expect(rows[0]?.textContent).not.toContain("acme.example");
    expect(rows[1]?.textContent).toContain("acme.example");
  });

  // SKY-15619 mutation L: `disabled={disabled || settled}` -> `disabled={false}` on the choice
  // buttons left the suite green, because the reload test above asserts aria-pressed and the
  // absence of Send but never that the controls are inert. A historical card that stayed
  // clickable would have shipped.
  it("mutation L: a historical card's controls are inert", () => {
    render(
      <QuestionPartsCard
        parts={mixed}
        answeredFrom={questionAnswerLine("Which day?", "Friday")}
        disabled
        onAnswer={vi.fn()}
      />,
    );

    for (const button of screen.getAllByRole("button")) {
      expect((button as HTMLButtonElement).disabled).toBe(true);
    }
    // No live field either: a read-only card renders none rather than a disabled empty box.
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  // SKY-15619 mutation N: `settled` -> `false` left the suite green, because no test rendered a
  // LIVE card that already carries an answer. Without the collapse, an answered question keeps
  // offering to answer itself again.
  it("mutation N: a live card that already carries an answer stops offering to answer again", () => {
    render(
      <QuestionPartsCard
        parts={mixed}
        answeredFrom={questionAnswerLine("Which day?", "Friday")}
        disabled={false}
        onAnswer={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Monday" }).hasAttribute("disabled"),
    ).toBe(true);
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

  // The claim-once receipt matches emitted labels against the parts it was handed, so it assumes
  // the rendered list IS the admitted list. parseQuestionParts drops entries with a non-string
  // part_id or prompt; if it ever silently shortened the list, answers would land on the wrong
  // rows. Pins that invariant on an envelope shaped exactly as the server admits.
  it("renders every part the server admitted, in order", () => {
    const admitted = [
      { part_id: "p1", prompt: "Which store?", choices: [] },
      { part_id: "p2", prompt: "Which items?", choices: ["Recent", "Saved"] },
      { part_id: "p3", prompt: "Which email?", choices: [] },
    ];

    const parsed = parseTerminalEnvelope({
      next_state: "awaiting_user_input",
      rendered_from_envelope: true,
      question_parts: admitted,
    })?.questionParts;

    expect(parsed).toEqual(admitted);
  });

  it("is empty on an envelope that carries no parts", () => {
    expect(
      parseTerminalEnvelope({ next_state: "stopped" })?.questionParts,
    ).toEqual([]);
  });
});
