import { afterEach, describe, expect, test } from "vitest";

import type { RunBlockingBlock } from "./getRunBlockingBlocks";
import { useRunValidationStore } from "./useRunValidationStore";

function block(overrides: Partial<RunBlockingBlock> = {}): RunBlockingBlock {
  return {
    id: "login-1",
    label: "Login",
    ancestorIds: [],
    path: [],
    ...overrides,
  };
}

describe("useRunValidationStore.setBlockingBlocks equality", () => {
  afterEach(() => {
    useRunValidationStore.getState().setBlockingBlocks([]);
  });

  test("bails out (keeps the same reference) when nothing changed", () => {
    const set = useRunValidationStore.getState().setBlockingBlocks;
    set([block()]);
    const first = useRunValidationStore.getState().blockingBlocks;
    // A fresh array of an equivalent block must not trigger a re-render.
    set([block()]);
    expect(useRunValidationStore.getState().blockingBlocks).toBe(first);
  });

  test("updates when a container ancestor is renamed (path label changes)", () => {
    const set = useRunValidationStore.getState().setBlockingBlocks;
    set([
      block({
        ancestorIds: ["loop-1"],
        path: [{ kind: "loop", label: "Loop A" }],
      }),
    ]);
    const first = useRunValidationStore.getState().blockingBlocks;
    set([
      block({
        ancestorIds: ["loop-1"],
        path: [{ kind: "loop", label: "Loop B" }],
      }),
    ]);
    expect(useRunValidationStore.getState().blockingBlocks).not.toBe(first);
    expect(
      useRunValidationStore.getState().blockingBlocks[0]?.path[0]?.label,
    ).toBe("Loop B");
  });

  test("updates when a block moves between branches of the same conditional", () => {
    const set = useRunValidationStore.getState().setBlockingBlocks;
    // Same conditional ancestor id, only the resolved branch differs.
    set([
      block({
        ancestorIds: ["cond-1"],
        path: [{ kind: "conditional", label: "Cond", branch: "if" }],
      }),
    ]);
    const first = useRunValidationStore.getState().blockingBlocks;
    set([
      block({
        ancestorIds: ["cond-1"],
        path: [{ kind: "conditional", label: "Cond", branch: "else" }],
      }),
    ]);
    expect(useRunValidationStore.getState().blockingBlocks).not.toBe(first);
    expect(
      useRunValidationStore.getState().blockingBlocks[0]?.path[0]?.branch,
    ).toBe("else");
  });

  test("updates when the blocks are reordered", () => {
    const set = useRunValidationStore.getState().setBlockingBlocks;
    const a = block({ id: "a", label: "A" });
    const b = block({ id: "b", label: "B" });
    set([a, b]);
    const first = useRunValidationStore.getState().blockingBlocks;
    set([b, a]);
    expect(useRunValidationStore.getState().blockingBlocks).not.toBe(first);
    expect(
      useRunValidationStore.getState().blockingBlocks.map((x) => x.id),
    ).toEqual(["b", "a"]);
  });
});
