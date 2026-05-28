// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { basicLocalTimeFormat } from "@/util/timeFormat";
import type { ObserverThought } from "../types/workflowRunTypes";
import { ThoughtCard } from "./ThoughtCard";

function buildThought(
  overrides: Partial<ObserverThought> = {},
): ObserverThought {
  return {
    thought_id: "th_default",
    user_input: null,
    observation: null,
    thought: "Considering whether to scroll the page",
    answer: null,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

describe("ThoughtCard", () => {
  it("renders the 'Thought' label and the thought body when there is content", () => {
    render(
      <ThoughtCard
        active={false}
        thought={buildThought()}
        onClick={() => {}}
      />,
    );
    expect(screen.getByText("Thought")).toBeDefined();
    expect(
      screen.getByText("Considering whether to scroll the page"),
    ).toBeDefined();
  });

  it("renders the thought start timestamp", () => {
    const thought = buildThought();

    render(<ThoughtCard active={false} thought={thought} onClick={() => {}} />);

    expect(
      screen.getByText(`Started ${basicLocalTimeFormat(thought.created_at)}`),
    ).toBeDefined();
  });

  it("renders 'Thinking' when there is no answer or thought yet", () => {
    render(
      <ThoughtCard
        active={false}
        thought={buildThought({ thought: null, answer: null })}
        onClick={() => {}}
      />,
    );
    expect(screen.getByText("Thinking")).toBeDefined();
  });

  it("prefers the answer over the thought when both are present", () => {
    render(
      <ThoughtCard
        active={false}
        thought={buildThought({
          thought: "intermediate thought",
          answer: "final answer text",
        })}
        onClick={() => {}}
      />,
    );
    expect(screen.getByText("final answer text")).toBeDefined();
    expect(screen.queryByText("intermediate thought")).toBeNull();
  });

  it("fires onClick when the card is clicked", () => {
    const onClick = vi.fn();
    render(
      <ThoughtCard active={false} thought={buildThought()} onClick={onClick} />,
    );
    fireEvent.click(screen.getByText("Thought"));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
