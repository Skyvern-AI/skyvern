import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { WorkflowHeaderCollapseTab } from "./WorkflowHeaderCollapseTab";

afterEach(cleanup);

describe("WorkflowHeaderCollapseTab", () => {
  test("renders the collapse label when expanded", () => {
    render(<WorkflowHeaderCollapseTab collapsed={false} onToggle={() => {}} />);
    // getByRole throws if missing; the call itself is the assertion.
    screen.getByRole("button", { name: "Collapse header" });
  });

  test("renders the expand label when collapsed", () => {
    render(<WorkflowHeaderCollapseTab collapsed={true} onToggle={() => {}} />);
    screen.getByRole("button", { name: "Expand header" });
  });

  test("invokes onToggle when clicked", () => {
    const onToggle = vi.fn();
    render(<WorkflowHeaderCollapseTab collapsed={false} onToggle={onToggle} />);
    fireEvent.click(screen.getByRole("button"));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  test("aria-expanded reflects the collapsed prop", () => {
    const { rerender } = render(
      <WorkflowHeaderCollapseTab collapsed={false} onToggle={() => {}} />,
    );
    expect(screen.getByRole("button").getAttribute("aria-expanded")).toBe(
      "true",
    );
    rerender(
      <WorkflowHeaderCollapseTab collapsed={true} onToggle={() => {}} />,
    );
    expect(screen.getByRole("button").getAttribute("aria-expanded")).toBe(
      "false",
    );
  });
});
