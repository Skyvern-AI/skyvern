// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import type { RunBlockingBlock } from "./getRunBlockingBlocks";
import { RunBlockingSurface } from "./RunBlockingSurface";
import { useLocateBlockStore } from "./useLocateBlockStore";
import { useRunBlockingPanelStore } from "./useRunBlockingPanelStore";
import { useRunValidationStore } from "./useRunValidationStore";

function block(
  id: string,
  label: string,
  path: RunBlockingBlock["path"] = [],
): RunBlockingBlock {
  return {
    id,
    label,
    ancestorIds: path.map((_, index) => `${id}-ancestor-${index}`),
    path,
  };
}

describe("RunBlockingSurface", () => {
  beforeEach(() => {
    useRunValidationStore.getState().setBlockingBlocks([]);
    useLocateBlockStore.getState().clearLocate();
    useRunBlockingPanelStore.getState().setCollapsed(false);
  });
  afterEach(() => {
    cleanup();
    useRunValidationStore.getState().setBlockingBlocks([]);
    useLocateBlockStore.getState().clearLocate();
    useRunBlockingPanelStore.getState().setCollapsed(false);
  });

  test("renders nothing when no blocks are run-blocking", () => {
    const { container } = render(<RunBlockingSurface />);
    expect(container.firstChild).toBeNull();
  });

  test("lists every blocking block", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([block("n2", "block_2"), block("n3", "block_3")]);
    render(<RunBlockingSurface />);
    expect(screen.getByText("2 blocks need fixing")).toBeTruthy();
    expect(screen.getByText("block_2")).toBeTruthy();
    expect(screen.getByText("block_3")).toBeTruthy();
  });

  test("shows a breadcrumb path for nested blocks", () => {
    useRunValidationStore.getState().setBlockingBlocks([
      block("n3", "block_3", [
        { kind: "loop", label: "loop_1" },
        { kind: "conditional", label: "cond_1", branch: "else" },
      ]),
    ]);
    render(<RunBlockingSurface />);
    expect(screen.getByText("loop_1")).toBeTruthy();
    expect(screen.getByText("cond_1 · else")).toBeTruthy();
  });

  test("clicking a block requests locate with its node id", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([block("n2", "block_2")]);
    render(<RunBlockingSurface />);
    fireEvent.click(screen.getByText("block_2"));
    expect(useLocateBlockStore.getState().request?.nodeId).toBe("n2");
  });

  test("collapses to a pill and expands again", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([block("n2", "block_2"), block("n3", "block_3")]);
    render(<RunBlockingSurface />);
    fireEvent.click(screen.getByLabelText("Collapse run-blocking panel"));
    expect(screen.queryByText("Resolve these before you can run")).toBeNull();
    // Pill keeps the count and re-expands on click.
    fireEvent.click(screen.getByText("2 blocks need fixing"));
    expect(screen.getByText("Resolve these before you can run")).toBeTruthy();
  });
});
