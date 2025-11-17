import { beforeAll, describe, expect, it, vi } from "vitest";

import type { BlockYAML } from "../../types/workflowYamlTypes";

let upgradeWorkflowDefinitionToVersionTwo: (typeof import("../workflowEditorUtils"))["upgradeWorkflowDefinitionToVersionTwo"];

beforeAll(async () => {
  const store: Record<string, string> = {};
  const localStorageMock: Storage = {
    get length() {
      return Object.keys(store).length;
    },
    clear: () => {
      Object.keys(store).forEach((key) => delete store[key]);
    },
    getItem: (key: string) => store[key] ?? null,
    key: (index: number) => Object.keys(store)[index] ?? null,
    removeItem: (key: string) => {
      delete store[key];
    },
    setItem: (key: string, value: string) => {
      store[key] = value;
    },
  };
  vi.stubGlobal("localStorage", localStorageMock);
  ({ upgradeWorkflowDefinitionToVersionTwo } = await import(
    "../workflowEditorUtils"
  ));
});

function waitBlock(label: string): BlockYAML {
  return {
    block_type: "wait",
    label,
    wait_sec: 1,
  };
}

describe("upgradeWorkflowDefinitionToVersionTwo", () => {
  it("assigns sequential next_block_label values for top-level blocks", () => {
    const originalBlocks: BlockYAML[] = [
      waitBlock("block_1"),
      waitBlock("block_2"),
      waitBlock("block_3"),
    ];

    const { blocks, version } =
      upgradeWorkflowDefinitionToVersionTwo(originalBlocks);

    expect(version).toBe(2);
    expect(blocks[0]?.next_block_label).toBe("block_2");
    expect(blocks[1]?.next_block_label).toBe("block_3");
    expect(blocks[2]?.next_block_label).toBeNull();
    expect(originalBlocks[0]?.next_block_label).toBeUndefined();
  });

  it("assigns sequential next_block_label values inside for loop blocks", () => {
    const originalBlocks: BlockYAML[] = [
      {
        block_type: "for_loop",
        label: "loop_1",
        loop_blocks: [waitBlock("inner_1"), waitBlock("inner_2")],
        loop_variable_reference: null,
        loop_over_parameter_key: "items",
        complete_if_empty: false,
      },
      waitBlock("after_loop"),
    ];

    const { blocks } = upgradeWorkflowDefinitionToVersionTwo(originalBlocks);

    const loopBlock = blocks[0];
    if (loopBlock?.block_type !== "for_loop") {
      throw new Error("Expected a for_loop block");
    }
    expect(loopBlock.next_block_label).toBe("after_loop");
    expect(loopBlock.loop_blocks[0]?.next_block_label).toBe("inner_2");
    expect(loopBlock.loop_blocks[1]?.next_block_label).toBeNull();
    expect(blocks[1]?.next_block_label).toBeNull();
  });

  it("preserves version greater than two and existing metadata", () => {
    const originalBlocks: BlockYAML[] = [
      {
        ...waitBlock("custom"),
        next_block_label: "already_set",
      },
    ];

    const { blocks, version } = upgradeWorkflowDefinitionToVersionTwo(
      originalBlocks,
      3,
    );

    expect(version).toBe(3);
    expect(blocks[0]?.next_block_label).toBe("already_set");
    expect(originalBlocks[0]?.next_block_label).toBe("already_set");
  });
});
