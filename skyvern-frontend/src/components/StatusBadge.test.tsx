// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { Status } from "@/api/types";

import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  test("always renders the status text, visually hidden below md", () => {
    render(<StatusBadge status={Status.Completed} />);

    const label = screen.getByText("completed");
    expect(label.classList.contains("sr-only")).toBe(true);
    expect(label.classList.contains("md:not-sr-only")).toBe(true);
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
