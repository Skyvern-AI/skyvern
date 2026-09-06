// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import { BlockStatusBar } from "./BlockStatusBar";

const mocks = vi.hoisted(() => ({
  timeline: undefined as unknown,
  isPlaceholderData: false,
}));

vi.mock("../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: () => ({
    data: mocks.timeline,
    isPlaceholderData: mocks.isPlaceholderData,
  }),
}));

function seedRunningBlock() {
  mocks.timeline = [
    {
      type: "block",
      block: {
        workflow_run_block_id: "wrb_1",
        block_type: "task",
        label: "checkout",
        status: Status.Running,
        actions: null,
      },
      children: [],
      thought: null,
      created_at: "2026-01-01T00:00:00Z",
      modified_at: "2026-01-01T00:00:00Z",
    },
  ];
}

function renderBar() {
  return render(
    <MemoryRouter initialEntries={["/agents/wpid_1/studio?wr=wr_2"]}>
      <BlockStatusBar blockLabel="checkout" />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  mocks.timeline = undefined;
  mocks.isPlaceholderData = false;
});

describe("BlockStatusBar", () => {
  it("shows no per-block status while the timeline still belongs to the previous run", () => {
    seedRunningBlock();
    mocks.isPlaceholderData = true;

    const { container } = renderBar();

    expect(container.textContent).toBe("");
  });

  it("shows the block status for the run in view", () => {
    seedRunningBlock();

    const { container } = renderBar();

    expect(container.textContent).toContain("Running");
  });
});
