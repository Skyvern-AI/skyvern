// @vitest-environment jsdom

import { Edge } from "@xyflow/react";
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import type { WorkflowBlock } from "@/routes/workflows/types/workflowTypes";
import { AppNode } from "../nodes";
import { useApplyRecordedBlocks } from "./useApplyRecordedBlocks";

const initialRecordedBlocksState = useRecordedBlocksStore.getState();

describe("useApplyRecordedBlocks", () => {
  afterEach(() => {
    useRecordedBlocksStore.setState(initialRecordedBlocksState, true);
  });

  it("applies recorded blocks when enabled in debugger/build mode", () => {
    const doLayout = vi.fn();
    const nodes = [{ id: "start", data: { label: "start" } }] as Array<AppNode>;
    const edges = [] as Array<Edge>;

    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "action",
            label: "click_button",
            title: "Click button",
            navigation_goal: "Click the button.",
            url: null,
            parameters: [],
          } as unknown as WorkflowBlock,
        ],
        parameters: [],
      },
      {
        previous: "start",
        next: null,
        connectingEdgeType: "edgeWithAddButton",
      },
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: true,
        nodes,
        edges,
        doLayout,
      }),
    );

    expect(doLayout).toHaveBeenCalledTimes(1);
    const layoutArgs = doLayout.mock.calls[0];
    expect(layoutArgs).toBeDefined();
    const [mergedNodes, mergedEdges] = layoutArgs!;
    expect(mergedNodes).toHaveLength(2);
    expect(mergedEdges.length).toBeGreaterThan(0);
  });

  it("does not apply recorded blocks when disabled", () => {
    const doLayout = vi.fn();

    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "goto_url",
            label: "goto_home",
            url: "https://example.com",
          } as unknown as WorkflowBlock,
        ],
        parameters: [],
      },
      {
        previous: null,
        next: null,
        connectingEdgeType: "default",
      },
    );

    renderHook(() =>
      useApplyRecordedBlocks({
        enabled: false,
        nodes: [],
        edges: [],
        doLayout,
      }),
    );

    expect(doLayout).not.toHaveBeenCalled();
  });

  it("clears a pending payload when the consumer unmounts (interrupted handoff)", () => {
    const doLayout = vi.fn();

    // enabled: false keeps the payload pending — mirrors a handoff that never
    // applied before the canvas went away (e.g. navigating right after commit).
    const { unmount } = renderHook(() =>
      useApplyRecordedBlocks({
        enabled: false,
        nodes: [],
        edges: [],
        doLayout,
      }),
    );

    useRecordedBlocksStore.getState().setRecordedBlocks(
      {
        blocks: [
          {
            block_type: "goto_url",
            label: "goto_home",
            url: "https://example.com",
          } as unknown as WorkflowBlock,
        ],
        parameters: [],
      },
      {
        previous: null,
        next: null,
        connectingEdgeType: "default",
      },
    );

    unmount();

    const state = useRecordedBlocksStore.getState();
    expect(state.blocks).toBeNull();
    expect(state.parameters).toBeNull();
    expect(state.insertionPoint).toBeNull();
  });
});
