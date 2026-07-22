// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { Status } from "@/api/types";

import { StatusBadge } from "./StatusBadge";

// No TooltipProvider wrapper on purpose: the collapsible badge self-provides one,
// so it must render without an ancestor provider (Radix Tooltip throws otherwise).
function renderCollapsible(status: Status | "pending") {
  return render(<StatusBadge status={status} collapsible />);
}

describe("StatusBadge", () => {
  test("always renders the status text, visually hidden below md", () => {
    render(<StatusBadge status={Status.Completed} />);

    const label = screen.getByText("completed");
    expect(label.classList.contains("sr-only")).toBe(true);
    expect(label.classList.contains("md:not-sr-only")).toBe(true);
  });

  test("keeps the label visible at every width when alwaysShowLabel is set", () => {
    render(<StatusBadge status={Status.Terminated} alwaysShowLabel />);

    const label = screen.getByText("terminated");
    expect(label.classList.contains("sr-only")).toBe(false);
    expect(label.classList.contains("md:not-sr-only")).toBe(false);
  });

  test("collapses to a compact pill below md via responsive classes", () => {
    const { container } = render(<StatusBadge status={Status.Completed} />);

    const badge = container.firstElementChild;
    expect(badge?.classList.contains("w-28")).toBe(false);
    expect(badge?.classList.contains("md:w-28")).toBe(true);
  });

  test("exposes the status text as a tooltip", () => {
    render(<StatusBadge status={Status.Running} />);

    expect(screen.getByTitle("running")).not.toBeNull();
  });

  test("humanizes timed_out", () => {
    render(<StatusBadge status={Status.TimedOut} />);

    expect(screen.getByText("timed out")).not.toBeNull();
    expect(screen.getByTitle("timed out")).not.toBeNull();
  });
});

describe("StatusBadge collapsible", () => {
  test("hides the label until the container is wide (@container, not viewport)", () => {
    renderCollapsible(Status.Completed);

    const label = screen.getByText("completed");
    expect(label.classList.contains("sr-only")).toBe(true);
    expect(
      label.classList.contains(
        "[@container_status_(min-width:384px)]:not-sr-only",
      ),
    ).toBe(true);
    expect(label.classList.contains("md:not-sr-only")).toBe(false);
  });

  test("sizes to the icon until the container is wide", () => {
    const { container } = renderCollapsible(Status.Completed);

    const badge = container.querySelector('[aria-label="completed"]');
    expect(
      badge?.classList.contains("[@container_status_(min-width:384px)]:w-28"),
    ).toBe(true);
    expect(badge?.classList.contains("md:w-28")).toBe(false);
  });

  test("keeps the full status as the accessible name when icon-only", () => {
    renderCollapsible(Status.Running);

    const badge = screen.getByLabelText("running");
    // role=img guarantees AT announces the aria-label for the icon-only pill
    expect(badge.getAttribute("role")).toBe("img");
  });

  test("with alwaysShowLabel the label stays visible (no sr-only collapse)", () => {
    render(
      <StatusBadge status={Status.Completed} collapsible alwaysShowLabel />,
    );

    const label = screen.getByText("completed");
    expect(label.classList.contains("sr-only")).toBe(false);
    expect(
      label.classList.contains(
        "[@container_status_(min-width:384px)]:not-sr-only",
      ),
    ).toBe(false);
  });

  test("wraps the badge in a keyboard-focusable tooltip trigger", () => {
    const { container } = renderCollapsible(Status.Completed);

    const trigger = container.querySelector("span.inline-flex.shrink-0");
    expect(trigger).not.toBeNull();
    expect(trigger?.getAttribute("tabindex")).toBe("0");
    expect(trigger?.querySelector('[aria-label="completed"]')).not.toBeNull();
  });

  test("drops the native title in favor of the tooltip", () => {
    renderCollapsible(Status.Completed);

    expect(screen.queryByTitle("completed")).toBeNull();
  });
});

describe("StatusBadge is pixel-identical when not collapsible", () => {
  test("keeps viewport breakpoints, native title, and no tooltip wrapper", () => {
    const { container } = render(<StatusBadge status={Status.Completed} />);

    const badge = container.firstElementChild;
    expect(badge?.getAttribute("aria-label")).toBeNull();
    expect(badge?.getAttribute("role")).toBeNull();
    expect(badge?.classList.contains("md:w-28")).toBe(true);
    expect(
      badge?.classList.contains("[@container_status_(min-width:384px)]:w-28"),
    ).toBe(false);
    expect(screen.getByTitle("completed")).not.toBeNull();
    expect(container.querySelector("span.inline-flex.shrink-0")).toBeNull();
  });
});
