// @vitest-environment jsdom

import { act, cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { REVEAL_MS_PER_CHAR } from "./actionReveal";
import { NarrativeView } from "./NarrativeView";
import { EMPTY_NARRATIVE, TurnNarrativeState } from "./narrativeState";

const NOW = new Date("2026-08-29T03:10:00Z").getTime();

function terminalTurn(
  overrides: Partial<TurnNarrativeState> = {},
): TurnNarrativeState {
  const terminalMessage =
    overrides.terminalMessage ?? "Copilot's terminal message.";
  return {
    ...EMPTY_NARRATIVE,
    turnId: "turn-prose",
    turnIndex: 0,
    terminal: "response",
    terminalMessage,
    narrativeSummary: overrides.narrativeSummary ?? terminalMessage,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

describe("NarrativeView terminal prose", () => {
  it("renders an answer as unboxed agent prose", () => {
    const { container } = render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "answer",
          terminalMessage: "Use a focused Code block for this download.",
        })}
      />,
    );

    const prose = screen.getByTestId("copilot-terminal-prose");
    expect(
      screen.getByTestId("copilot-terminal-prose-visual").textContent,
    ).toBe("Use a focused Code block for this download.");
    expect(prose.className).toContain("text-foreground");
    expect(screen.queryByText("Answered")).toBeNull();
    expect(container.querySelector(".rounded-xl")).toBeNull();
  });

  it("keeps an inspected Q&A answer in prose when no workflow evidence was produced", () => {
    render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "answer",
          terminalMessage:
            "Use a **For Loop** with a `websites` workflow input.",
          designStarted: true,
          designEnded: true,
          designActivity: [
            {
              kind: "tool_result",
              toolName: "get_workflow",
              text: "The workflow is empty.",
              success: true,
              iteration: 0,
              id: "tr-inspect-workflow",
            },
          ],
        })}
      />,
    );

    expect(screen.getByTestId("copilot-terminal-prose")).toBeTruthy();
    const visual = screen.getByTestId("copilot-terminal-prose-visual");
    expect(
      within(visual).getByText("For Loop", { selector: "strong" }),
    ).toBeTruthy();
    expect(
      within(visual).getByText("websites", { selector: "code" }),
    ).toBeTruthy();
    expect(screen.queryByText("Answered")).toBeNull();
  });

  it("reveals fresh terminal prose with a gradient edge while keeping the full answer accessible", () => {
    const answer = "Use a focused Code block for this download.";
    render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "answer",
          terminalMessage: answer,
          endedAt: new Date(NOW).toISOString(),
        })}
      />,
    );

    const visual = screen.getByTestId("copilot-terminal-prose-visual");
    expect(visual.getAttribute("aria-hidden")).toBe("true");
    expect(visual.textContent!.length).toBeLessThan(answer.length);
    expect(
      Number(
        screen
          .getByTestId("copilot-terminal-prose-gradient")
          .getAttribute("style")
          ?.match(/[\d.]+/)?.[0],
      ),
    ).toBeLessThan(1);
    expect(screen.getByText(answer, { selector: ".sr-only p" })).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(answer.length * REVEAL_MS_PER_CHAR);
    });

    expect(visual.textContent).toBe(answer);
    expect(screen.getByTestId("copilot-terminal-prose-gradient")).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(1_000);
    });

    expect(screen.queryByTestId("copilot-terminal-prose-gradient")).toBeNull();
  });

  it("renders a clarification in the single accent treatment", () => {
    render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "clarify",
          terminalMessage: "Which Coursera URL should I use?",
        })}
      />,
    );

    const prose = screen.getByTestId("copilot-terminal-prose");
    expect(
      screen.getByTestId("copilot-terminal-prose-visual").textContent,
    ).toBe("Which Coursera URL should I use?");
    expect(prose.className).toContain("border-l-2");
    expect(prose.className).toContain("dark:text-[#a7ccdd]");
    expect(screen.queryByText("Needs your input")).toBeNull();
  });

  it("renders terminal prose as safe Markdown once it has settled", () => {
    render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "answer",
          terminalMessage:
            "Use **records** with `for_loop`.\n\n- One item per iteration\n- Keep the JSON expanded\n\n[Docs](https://docs.skyvern.com) [unsafe](javascript:alert(1)) ![remote tracker](https://example.test/tracker.png)",
        })}
      />,
    );

    const visual = screen.getByTestId("copilot-terminal-prose-visual");
    expect(
      within(visual).getByText("records", { selector: "strong" }),
    ).toBeTruthy();
    expect(
      within(visual).getByText("for_loop", { selector: "code" }),
    ).toBeTruthy();
    expect(
      within(visual).getByText("One item per iteration", { selector: "li" }),
    ).toBeTruthy();
    expect(screen.queryByText("**records**")).toBeNull();
    expect(
      screen
        .getByText("records", { selector: ".sr-only strong" })
        .closest(".whitespace-normal"),
    ).toBeTruthy();
    expect(
      visual.querySelector('a[href="https://docs.skyvern.com"]')?.textContent,
    ).toBe("Docs");
    expect(visual.querySelectorAll("a")[1]?.getAttribute("href")).toBe("");
    expect(visual.querySelector("img")).toBeNull();
  });

  it("renders Markdown structure while the response is still revealing", () => {
    const answer = "Use **records** with `for_loop` for each item.";
    render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "answer",
          terminalMessage: answer,
          endedAt: new Date(NOW).toISOString(),
        })}
      />,
    );

    act(() => {
      vi.advanceTimersByTime(6 * REVEAL_MS_PER_CHAR);
    });

    const visual = screen.getByTestId("copilot-terminal-prose-visual");
    expect(visual.textContent!.length).toBeLessThan(answer.length);
    expect(visual.querySelector("strong")?.textContent).toBe("re");
    expect(visual.textContent).not.toContain("**");
  });

  it("uses the typed legacy question signal without parsing the message text", () => {
    render(
      <NarrativeView
        turn={terminalTurn({
          responseType: "ASK_QUESTION",
          terminalMessage: "Please choose a login method.",
        })}
      />,
    );

    expect(screen.getByTestId("copilot-terminal-prose").className).toContain(
      "border-l-2",
    );
  });

  it("keeps the run outcome fallback when a clarification has no owning row", () => {
    const { container } = render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "clarify",
          responseType: "REPLY",
          terminalMessage: "Which **login** should I use?",
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: "The sign-in flow did not reach the expected page.",
          },
        })}
      />,
    );

    expect(screen.queryByTestId("copilot-terminal-prose")).toBeNull();
    expect(screen.getByText("login", { selector: "strong" })).toBeTruthy();
    expect(screen.getByText("Outcome not confirmed")).toBeTruthy();
    expect(
      screen.getByText(/The sign-in flow did not reach the expected page/),
    ).toBeTruthy();
    expect(container.querySelector(".rounded-xl")).toBeNull();
  });

  it("keeps the question treatment on an ask that carries recorded evidence", () => {
    render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "clarify",
          responseType: "REPLY",
          terminalMessage: "Which **login** should I use?",
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: "The sign-in flow did not reach the expected page.",
          },
        })}
      />,
    );

    // The ask reads as an ask, and the recorded outcome it followed survives.
    expect(screen.getByTestId("copilot-detail-prose").className).toContain(
      "border-l-2",
    );
    expect(screen.getByText("login", { selector: "strong" })).toBeTruthy();
    expect(screen.getByText("Outcome not confirmed")).toBeTruthy();
  });

  it("warns on a reloaded unconfirmed verdict that carries no reason text", () => {
    render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "build",
          terminalMessage: "I built and tested the navigation block.",
          turnFacts: {
            factsAvailable: true,
            evaluationState: "not_demonstrated",
            runId: "wr_reloaded",
            runCompleted: true,
            terminalCause: null,
            blocksRunThisTurn: 1,
            ranCleanOnCurrentSource: false,
            authoredBlockCount: 1,
            matchingSourceBlockCount: 1,
          },
        })}
      />,
    );

    expect(screen.getByText("Outcome not confirmed")).toBeTruthy();
    expect(
      screen.getByText(/the run finished without showing the goal was met/),
    ).toBeTruthy();
  });

  it("keeps a reloaded demonstrated run out of the unconfirmed warning", () => {
    render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "build",
          terminalMessage: "I built and tested the navigation block.",
          turnFacts: {
            factsAvailable: true,
            evaluationState: "demonstrated",
            runId: "wr_reloaded",
            runCompleted: true,
            terminalCause: null,
            blocksRunThisTurn: 1,
            ranCleanOnCurrentSource: true,
            authoredBlockCount: 1,
            matchingSourceBlockCount: 1,
          },
          blocks: [
            {
              workflowRunBlockId: "wrb-open-site",
              label: "open_site",
              blockType: "navigation",
              state: "completed",
              outcome: "not_demonstrated",
              lastSeenIteration: 0,
              activity: [],
              startedAt: null,
              endedAt: null,
            },
          ],
        })}
      />,
    );

    expect(screen.queryByText("Outcome not confirmed")).toBeNull();
  });

  it("does not give a cancelled turn the question rail", () => {
    render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "clarify",
          responseType: "ASK_QUESTION",
          cancelled: true,
          terminalMessage: "Which login should I use?",
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: "The sign-in flow did not reach the expected page.",
          },
        })}
      />,
    );

    expect(screen.getByTestId("copilot-detail-prose").className).not.toContain(
      "border-l-2",
    );
  });

  it("moves a run-level unconfirmed reason into its failed activity row", () => {
    render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "build",
          terminalMessage: "I built and tested the navigation block.",
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: "The expected destination was not observed.",
          },
          blocks: [
            {
              workflowRunBlockId: "wrb-open-site",
              label: "open_site",
              blockType: "navigation",
              state: "failed",
              lastSeenIteration: 0,
              activity: [],
              startedAt: null,
              endedAt: null,
            },
          ],
        })}
      />,
    );

    expect(screen.queryByText("Outcome not confirmed")).toBeNull();
    expect(
      screen.getByText(/The expected destination was not observed/),
    ).toBeTruthy();
  });

  it("keeps a run-level unconfirmed outcome when completed blocks have no verdict", () => {
    render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "build",
          terminalMessage: "I built and tested the navigation block.",
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: "The expected destination was not observed.",
          },
          blocks: [
            {
              workflowRunBlockId: "wrb-open-site",
              label: "open_site",
              blockType: "navigation",
              state: "completed",
              lastSeenIteration: 0,
              activity: [],
              startedAt: null,
              endedAt: null,
            },
          ],
        })}
      />,
    );

    expect(screen.getByText("Outcome not confirmed")).toBeTruthy();
    expect(
      screen.getByText(/The expected destination was not observed/),
    ).toBeTruthy();
  });

  it("keeps a run-level unconfirmed outcome while a terminal block is still evaluating", () => {
    render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "build",
          terminalMessage: "I built and tested the navigation block.",
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: "The expected destination was not observed.",
          },
          blocks: [
            {
              workflowRunBlockId: "wrb-open-site",
              label: "open_site",
              blockType: "navigation",
              state: "completed",
              outcome: "evaluating",
              lastSeenIteration: 0,
              activity: [],
              startedAt: null,
              endedAt: null,
            },
          ],
        })}
      />,
    );

    expect(screen.getByText("Outcome not confirmed")).toBeTruthy();
    expect(
      screen.getByText(/The expected destination was not observed/),
    ).toBeTruthy();
  });

  it("keeps a build result in its structured detail view", () => {
    const { container } = render(
      <NarrativeView
        turn={terminalTurn({
          responseKind: "build",
          terminalMessage:
            "I built the **navigation** block. [Read the docs](https://docs.skyvern.com)",
          designStarted: true,
          draft: {
            blockCount: 1,
            blockLabels: ["open_site"],
            summary: null,
          },
          blocks: [
            {
              workflowRunBlockId: "wrb-open-site",
              label: "open_site",
              blockType: "navigation",
              state: "completed",
              lastSeenIteration: 0,
              activity: [],
              startedAt: null,
              endedAt: null,
            },
          ],
        })}
      />,
    );

    expect(screen.queryByTestId("copilot-terminal-prose")).toBeNull();
    const formattedText = screen.getByText("navigation", {
      selector: "strong",
    });
    expect(formattedText).toBeTruthy();
    expect(formattedText.closest(".pl-9")).toBeNull();
    expect(formattedText.closest(".pr-8")).toBeNull();
    expect(screen.getAllByText("Open Site").length).toBeGreaterThan(0);
    expect(container.querySelector(".rounded-xl")).toBeNull();
    const docsLink = screen.getByRole("link", { name: "Read the docs" });
    expect(docsLink.closest("button")).toBeNull();

    expect(screen.queryByRole("button", { name: "Collapse turn" })).toBeNull();
  });
});
