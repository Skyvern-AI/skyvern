import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { OnboardingProgressCard } from "./OnboardingProgressCard";

const forbiddenCopy =
  /heuristic|reward|credential|api|mcp|channel|schedule|questionnaire|social|follow|role|company|referral|credits?|points?|survey/i;
it.each([
  [
    0,
    false,
    "first_agent_created",
    "Use the working example",
    "#working-example-heading",
    "How to build an agent",
    "https://www.skyvern.com/docs/cloud/building-agents/build-an-agent",
  ],
  [
    1,
    true,
    "first_successful_run",
    "Run agent",
    "/agents",
    "How to run an agent",
    "https://www.skyvern.com/docs/cloud/building-agents/run-an-agent",
  ],
] as const)(
  "renders %s progress with its primary destination",
  (
    completedCount,
    firstMilestoneComplete,
    nextActionKey,
    primaryLabel,
    href,
    resourceLabel,
    resourceHref,
  ) => {
    const onDismiss = vi.fn();
    render(
      <OnboardingProgressCard
        state="active"
        completedCount={completedCount}
        firstMilestoneComplete={firstMilestoneComplete}
        nextActionKey={nextActionKey}
        isPending={false}
        onDismiss={onDismiss}
      />,
      { wrapper: href === "/agents" ? MemoryRouter : undefined },
    );
    const primaryLink = screen.getByRole("link", { name: primaryLabel });
    expect(primaryLink.getAttribute("href")).toBe(href);
    const resourceLink = screen.getByRole("link", { name: resourceLabel });
    expect(resourceLink.getAttribute("href")).toBe(resourceHref);
    expect(screen.getByRole("region").textContent).not.toMatch(forbiddenCopy);
    if (firstMilestoneComplete) {
      expect(screen.getByText("Complete:").className).toContain("sr-only");
    } else {
      expect(screen.queryByText("Complete:")).toBeNull();
    }
    fireEvent.click(screen.getByRole("button", { name: "Hide setup" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  },
);
