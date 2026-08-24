// @vitest-environment jsdom

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { BlockDetailHeader } from "./shared";

function buildBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_default",
    workflow_run_id: "wr_default",
    parent_workflow_run_block_id: null,
    block_type: "task",
    label: null,
    description: null,
    title: null,
    status: Status.Completed,
    failure_reason: null,
    output: null,
    continue_on_failure: false,
    task_id: null,
    url: null,
    navigation_goal: null,
    navigation_payload: null,
    data_extraction_goal: null,
    data_schema: null,
    terminate_criterion: null,
    complete_criterion: null,
    include_action_history_in_verification: null,
    engine: null,
    actions: null,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    duration: null,
    loop_values: null,
    current_value: null,
    current_index: null,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

describe("BlockDetailHeader iterated-value chip", () => {
  it("shows the block id beside the block label", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_531711929286793050",
      block_type: "http_request",
      label: "signin",
    });

    render(<BlockDetailHeader block={block} runFinalized />);

    expect(screen.getByText("HTTP Request")).toBeDefined();
    expect(screen.getByText("signin")).toBeDefined();
    expect(screen.getByText("wrb_531711929286793050")).toBeDefined();
  });

  it("shows loop_values[iterationOverride] on a for_loop with an explicit iteration selection", () => {
    const block = buildBlock({
      block_type: "for_loop",
      loop_values: ["alpha", "beta", "gamma"],
      // Backend mirrors the latest iteration on the loop block itself; we
      // should NOT use this when an older iteration is selected.
      current_value: "gamma",
      current_index: 2,
    });

    render(
      <BlockDetailHeader block={block} iterationOverride={1} runFinalized />,
    );

    expect(screen.getByText(/Iteration 2/)).toBeDefined();
    expect(screen.getByText("Iterated value:")).toBeDefined();
    expect(screen.getByText("beta")).toBeDefined();
    expect(screen.queryByText("gamma")).toBeNull();
  });

  it("falls back to current_value on a loop block with no iteration override", () => {
    const block = buildBlock({
      block_type: "for_loop",
      loop_values: ["alpha", "beta", "gamma"],
      current_value: "gamma",
      current_index: 2,
    });

    render(
      <BlockDetailHeader block={block} iterationOverride={null} runFinalized />,
    );

    expect(screen.getByText(/Iteration 3/)).toBeDefined();
    expect(screen.getByText("gamma")).toBeDefined();
  });

  it("hides iteration context on a while_loop with an iteration override but no loop_values", () => {
    const block = buildBlock({
      block_type: "while_loop",
      loop_values: null,
      // Backend may still mirror a latest value on the loop block; rather
      // than display it under a mismatched iteration label, render nothing.
      current_value: "latest",
      current_index: 4,
    });

    render(
      <BlockDetailHeader block={block} iterationOverride={2} runFinalized />,
    );

    expect(screen.queryByText(/Iteration 3/)).toBeNull();
    expect(screen.queryByText("Iterated value:")).toBeNull();
    expect(screen.queryByText("latest")).toBeNull();
  });

  it("hides iteration context when an explicit loop iteration is out of range", () => {
    const block = buildBlock({
      block_type: "for_loop",
      loop_values: ["alpha"],
      current_value: "alpha",
      current_index: 0,
    });

    render(
      <BlockDetailHeader block={block} iterationOverride={99} runFinalized />,
    );

    expect(screen.queryByText(/Iteration 100/)).toBeNull();
    expect(screen.queryByText("Iterated value:")).toBeNull();
    expect(screen.queryByText("alpha")).toBeNull();
  });

  it("leaves loop context to the loop block for a block inside a loop", () => {
    // The backend mirrors the loop's current_index/current_value onto every
    // child. Restating them here duplicates the timeline's "Iteration 1 ·
    // alpha" row, which is the ancestor of whatever child/action is selected.
    const block = buildBlock({
      block_type: "task",
      current_value: "alpha",
      current_index: 0,
    });

    render(
      <BlockDetailHeader block={block} iterationOverride={null} runFinalized />,
    );

    expect(screen.queryByText(/Iteration 1/)).toBeNull();
    expect(screen.queryByText("Iterated value:")).toBeNull();
    expect(screen.queryByText("alpha")).toBeNull();
  });
});

describe("BlockDetailHeader two-tier hierarchy", () => {
  it("leads with the block's own name and demotes its type and id to the meta row", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_531711929286793050",
      block_type: "http_request",
      label: "signin",
      status: Status.Completed,
    });

    const { container } = render(
      <BlockDetailHeader block={block} runFinalized />,
    );

    const primary = container.querySelector(
      '[data-slot="block-detail-header-primary"]',
    );
    const meta = container.querySelector(
      '[data-slot="block-detail-header-meta"]',
    );
    expect(primary).not.toBeNull();
    expect(meta).not.toBeNull();

    const primaryText = primary?.textContent ?? "";
    const metaText = meta?.textContent ?? "";

    // Primary row is the glance: identity + outcome only. Identity is the
    // user's label, as in the timeline row; the outcome is the row's glyph,
    // named for assistive tech rather than spelled out.
    expect(primaryText).toContain("signin");
    expect(primaryText).not.toContain("HTTP Request");
    expect(
      within(primary as HTMLElement).getByRole("img", { name: "completed" }),
    ).toBeDefined();

    // Type and UUID are classification and debug detail — meta row.
    expect(primaryText).not.toContain("wrb_531711929286793050");
    expect(metaText).toContain("HTTP Request");
    expect(metaText).toContain("wrb_531711929286793050");
  });
});

describe("BlockDetailHeader status placement", () => {
  it("leads with the timeline row's status glyph instead of trailing a labeled pill", () => {
    const block = buildBlock({ status: Status.Failed, duration: 2100 });

    render(<BlockDetailHeader block={block} runFinalized />);

    const glyph = screen.getByRole("img", { name: "failed" });
    const title = screen.getByText("Browser Task");
    // Status precedes the title in DOM order, as it does in the row above.
    expect(
      glyph.compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.queryByText(/^failed$/i)).toBeNull();
  });

  it("does not spin a Running block once the run is finalized", () => {
    const block = buildBlock({ status: Status.Running });

    const { container } = render(
      <BlockDetailHeader block={block} runFinalized />,
    );

    expect(container.querySelector(".animate-spin")).toBeNull();
  });
});
