// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { BrowserSessionOccupiedBy } from "./BrowserSessionOccupiedBy";

vi.mock("@/routes/workflows/editor/Workspace", () => ({
  CopyText: () => null,
}));

afterEach(cleanup);

function renderBadge(runnableId: string) {
  render(
    <MemoryRouter
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <BrowserSessionOccupiedBy runnableId={runnableId} />
    </MemoryRouter>,
  );
}

describe("BrowserSessionOccupiedBy", () => {
  it("links a workflow run to its run detail page", () => {
    renderBadge("wr_123");
    expect(screen.getByText("In use by")).toBeTruthy();
    const link = screen.getByRole("link", { name: "wr_123" });
    expect(link.getAttribute("href")).toBe("/runs/wr_123");
  });

  it("links a task run to its run detail page", () => {
    renderBadge("tsk_456");
    const link = screen.getByRole("link", { name: "tsk_456" });
    expect(link.getAttribute("href")).toBe("/runs/tsk_456");
  });
});
