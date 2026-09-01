// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";

import { OnboardingProgressBand } from "./OnboardingProgressBand";

const activeProgress = (completedCount: 0 | 1) => ({
  state: "active" as const,
  completed_count: completedCount,
  next_action_key:
    completedCount === 0
      ? ("first_agent_created" as const)
      : ("first_successful_run" as const),
  items: [
    {
      key: "first_agent_created" as const,
      completed_at: completedCount === 0 ? null : "2026-08-20T12:00:00Z",
    },
    { key: "first_successful_run" as const, completed_at: null },
  ],
});

const completedProgress = {
  state: "completed" as const,
};

const forbiddenCopy =
  /heuristic|reward|credential|api|mcp|channel|schedule|questionnaire|social|follow|role|company|referral|credits?|points?|survey/i;

it.each([
  [
    0,
    "1 of 3",
    "Describe your first agent",
    "How to build an agent",
    "https://www.skyvern.com/docs/cloud/building-agents/build-an-agent",
  ],
  [
    1,
    "2 of 3",
    "Run agent",
    "How to run an agent",
    "https://www.skyvern.com/docs/cloud/building-agents/run-an-agent",
  ],
] as const)(
  "renders %s server milestones as endowed progress",
  (completedCount, displayCount, primaryLabel, resourceLabel, resourceHref) => {
    const onDismiss = vi.fn();
    const onDescribeAgent = vi.fn();
    render(
      <OnboardingProgressBand
        progress={activeProgress(completedCount)}
        isPending={false}
        onDismiss={onDismiss}
        onRestore={vi.fn()}
        onDescribeAgent={onDescribeAgent}
      />,
      { wrapper: MemoryRouter },
    );

    expect(screen.getByText(displayCount)).toBeTruthy();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe(
      String(completedCount + 1),
    );
    const accountRow = screen.getByText("Account created").parentElement;
    expect(accountRow?.textContent).toContain("Complete: Account created");
    expect(accountRow?.querySelector(".sr-only")?.textContent).toBe(
      "Complete: ",
    );
    const primaryAction = screen.getByRole(
      completedCount === 0 ? "button" : "link",
      { name: primaryLabel },
    );
    if (completedCount === 0) {
      fireEvent.click(primaryAction);
      expect(onDescribeAgent).toHaveBeenCalledOnce();
    } else {
      expect(primaryAction.getAttribute("href")).toBe("/agents");
      expect(onDescribeAgent).not.toHaveBeenCalled();
    }
    expect(
      screen.getByRole("link", { name: resourceLabel }).getAttribute("href"),
    ).toBe(resourceHref);
    expect(screen.getByRole("region").textContent).not.toMatch(forbiddenCopy);
    expect(
      screen.getByText(
        "An agent is a browser automation you describe in plain words.",
      ),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Hide setup" }));
    expect(onDismiss).toHaveBeenCalledOnce();
  },
);

it("announces pending progress and prevents another visibility mutation", () => {
  const onDismiss = vi.fn();
  render(
    <OnboardingProgressBand
      progress={activeProgress(0)}
      isPending
      onDismiss={onDismiss}
      onRestore={vi.fn()}
      onDescribeAgent={vi.fn()}
    />,
    { wrapper: MemoryRouter },
  );

  expect(screen.getByRole("region").getAttribute("aria-busy")).toBe("true");
  expect(
    screen.getByText("Saving setup progress…").getAttribute("aria-live"),
  ).toBe("polite");
  const hide = screen.getByRole("button", { name: "Hide setup" });
  expect(hide.getAttribute("aria-disabled")).toBe("true");
  expect(
    screen
      .getByRole("button", { name: "Describe your first agent" })
      .hasAttribute("disabled"),
  ).toBe(true);
  expect(
    screen
      .getByRole("button", { name: "How to build an agent" })
      .hasAttribute("disabled"),
  ).toBe(true);
  fireEvent.click(hide);
  expect(onDismiss).not.toHaveBeenCalled();
});

it("restores a dismissed band", () => {
  const onRestore = vi.fn();
  render(
    <OnboardingProgressBand
      progress={{ state: "dismissed" }}
      isPending={false}
      onDismiss={vi.fn()}
      onRestore={onRestore}
      onDescribeAgent={vi.fn()}
    />,
    { wrapper: MemoryRouter },
  );

  expect(screen.queryByText("Build your first agent")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Resume setup" }));
  expect(onRestore).toHaveBeenCalledOnce();
});

it("celebrates only an active to completed transition", () => {
  vi.useFakeTimers();
  const props = {
    isPending: false,
    onDismiss: vi.fn(),
    onRestore: vi.fn(),
    onDescribeAgent: vi.fn(),
  };
  const view = render(
    <MemoryRouter>
      <OnboardingProgressBand progress={activeProgress(1)} {...props} />
    </MemoryRouter>,
  );

  view.rerender(
    <MemoryRouter>
      <OnboardingProgressBand progress={completedProgress} {...props} />
    </MemoryRouter>,
  );
  expect(screen.getByRole("status").textContent).toContain("3 of 3 complete");
  expect(screen.getByText("First agent ready")).toBeTruthy();

  act(() => vi.advanceTimersByTime(1800));
  expect(screen.queryByText("First agent ready")).toBeNull();
  vi.useRealTimers();
});

it.each([
  [{ state: "completed" }],
  [{ state: "ineligible" }],
  [{ state: "unknown" }],
  [null],
])("fails closed for non-renderable progress %#", (progress) => {
  const { container } = render(
    <OnboardingProgressBand
      progress={progress}
      isPending={false}
      onDismiss={vi.fn()}
      onRestore={vi.fn()}
      onDescribeAgent={vi.fn()}
    />,
    { wrapper: MemoryRouter },
  );
  expect(container.innerHTML).toBe("");
});

it("offers the working example as a quiet focus handoff from step 2", () => {
  render(
    <OnboardingProgressBand
      progress={activeProgress(0)}
      isPending={false}
      onDismiss={vi.fn()}
      onRestore={vi.fn()}
      onDescribeAgent={vi.fn()}
    >
      <h2 id="working-example-heading" tabIndex={-1}>
        See how an agent run is organized
      </h2>
    </OnboardingProgressBand>,
    { wrapper: MemoryRouter },
  );

  const affordance = screen.getByRole("link", {
    name: "or copy a working example",
  });
  expect(affordance.getAttribute("href")).toBe("#working-example-heading");
  fireEvent.click(affordance);
  expect(document.activeElement).toBe(
    screen.getByRole("heading", { name: "See how an agent run is organized" }),
  );
});
