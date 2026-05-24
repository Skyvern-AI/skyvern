import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";

import { WorkflowScopeContext } from "../WorkflowScopeContext";

import {
  makeCollapseKey,
  useIsBlockCollapsed,
  useNodeCollapseStore,
} from "./useNodeCollapseStore";

function resetStore() {
  useNodeCollapseStore.setState({ collapsed: {} });
}

beforeEach(() => {
  resetStore();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

const WF = "wf_test";

describe("useNodeCollapseStore (SKY-9069 / SKY-9361)", () => {
  test("initial state has no collapsed blocks", () => {
    expect(useNodeCollapseStore.getState().collapsed).toEqual({});
  });

  test("unknown labels read as not-collapsed by default", () => {
    const store = useNodeCollapseStore.getState();
    expect(Boolean(store.collapsed[`${WF}\x1funknown`])).toBe(false);
  });

  test("toggleBlock flips a label from expanded to collapsed and back", () => {
    const { toggleBlock } = useNodeCollapseStore.getState();
    toggleBlock(WF, "alpha");
    expect(useNodeCollapseStore.getState().collapsed[`${WF}\x1falpha`]).toBe(
      true,
    );
    toggleBlock(WF, "alpha");
    expect(`${WF}\x1falpha` in useNodeCollapseStore.getState().collapsed).toBe(
      false,
    );
  });

  test("toggleBlock is independent across labels", () => {
    const { toggleBlock } = useNodeCollapseStore.getState();
    toggleBlock(WF, "alpha");
    toggleBlock(WF, "beta");
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      [`${WF}\x1falpha`]: true,
      [`${WF}\x1fbeta`]: true,
    });
    toggleBlock(WF, "alpha");
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      [`${WF}\x1fbeta`]: true,
    });
  });

  test("collapseAll sets every passed label in the workflow to true", () => {
    const { collapseAll } = useNodeCollapseStore.getState();
    collapseAll(WF, ["a", "b", "c"]);
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      [`${WF}\x1fa`]: true,
      [`${WF}\x1fb`]: true,
      [`${WF}\x1fc`]: true,
    });
  });

  test("collapseAll preserves entries from other workflows", () => {
    useNodeCollapseStore.setState({
      collapsed: {
        "wf_other\x1fkeep": true,
        [`${WF}\x1fexisting`]: false,
      },
    });
    const { collapseAll } = useNodeCollapseStore.getState();
    collapseAll(WF, ["existing", "fresh"]);
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      "wf_other\x1fkeep": true,
      [`${WF}\x1fexisting`]: true,
      [`${WF}\x1ffresh`]: true,
    });
  });

  test("expandAll clears entries only within the given workflow", () => {
    const { collapseAll, expandAll } = useNodeCollapseStore.getState();
    collapseAll("wf_other", ["x"]);
    collapseAll(WF, ["a", "b"]);
    expandAll(WF);
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      "wf_other\x1fx": true,
    });
  });

  test("pruneStaleLabels drops entries whose labels are no longer present", () => {
    const { collapseAll, pruneStaleLabels } = useNodeCollapseStore.getState();
    collapseAll("wf_other", ["keep"]);
    collapseAll(WF, ["alpha", "beta", "gamma"]);
    pruneStaleLabels(WF, ["alpha"]);
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      "wf_other\x1fkeep": true,
      [`${WF}\x1falpha`]: true,
    });
  });

  test("pruneStaleLabels preserves entries in other workflows", () => {
    const { collapseAll, pruneStaleLabels } = useNodeCollapseStore.getState();
    collapseAll("wf_other", ["x", "y"]);
    collapseAll(WF, ["a"]);
    pruneStaleLabels(WF, []);
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      "wf_other\x1fx": true,
      "wf_other\x1fy": true,
    });
  });
});

function Probe({ label }: { label: string }) {
  const collapsed = useIsBlockCollapsed(label);
  return <div data-testid="state">{collapsed ? "1" : "0"}</div>;
}

describe("useNodeCollapseStore - workflow scoping", () => {
  test("collapsed state is namespaced by workflow id", () => {
    const { rerender } = render(
      <WorkflowScopeContext.Provider
        value={{ workflowId: "wf_a", readOnly: false }}
      >
        <Probe label="step1" />
      </WorkflowScopeContext.Provider>,
    );
    act(() => {
      useNodeCollapseStore.getState().toggleBlock("wf_a", "step1");
    });
    expect(screen.getByTestId("state").textContent).toBe("1");

    rerender(
      <WorkflowScopeContext.Provider
        value={{ workflowId: "wf_b", readOnly: false }}
      >
        <Probe label="step1" />
      </WorkflowScopeContext.Provider>,
    );
    expect(screen.getByTestId("state").textContent).toBe("0");
  });

  test("falls back to __global__ scope when no provider", () => {
    render(<Probe label="step1" />);
    act(() => {
      useNodeCollapseStore.getState().toggleBlock("__global__", "step1");
    });
    expect(screen.getByTestId("state").textContent).toBe("1");
  });

  test("read-only scope ignores persisted collapse state", () => {
    act(() => {
      useNodeCollapseStore.getState().toggleBlock("wf_a", "step1");
    });
    const { rerender } = render(
      <WorkflowScopeContext.Provider
        value={{ workflowId: "wf_a", readOnly: false }}
      >
        <Probe label="step1" />
      </WorkflowScopeContext.Provider>,
    );
    expect(screen.getByTestId("state").textContent).toBe("1");

    rerender(
      <WorkflowScopeContext.Provider
        value={{ workflowId: "wf_a", readOnly: true }}
      >
        <Probe label="step1" />
      </WorkflowScopeContext.Provider>,
    );
    expect(screen.getByTestId("state").textContent).toBe("0");
  });
});

describe("useNodeCollapseStore - persistence", () => {
  test("persists to localStorage under skyvern:node-collapse", () => {
    act(() => {
      useNodeCollapseStore.getState().toggleBlock("wf_x", "stepA");
    });
    const raw = localStorage.getItem("skyvern:node-collapse");
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw!);
    expect(parsed.state.collapsed["wf_x\x1fstepA"]).toBe(true);
  });
});

describe("renameBlock", () => {
  test("moves the collapsed entry from oldLabel to newLabel within the same workflow", () => {
    const store = useNodeCollapseStore.getState();
    act(() => {
      store.toggleBlock("wf-1", "Old Label");
    });
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-1", "Old Label")
      ],
    ).toBe(true);

    act(() => {
      useNodeCollapseStore
        .getState()
        .renameBlock("wf-1", "Old Label", "New Label");
    });

    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-1", "Old Label")
      ],
    ).toBeUndefined();
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-1", "New Label")
      ],
    ).toBe(true);
  });

  test("no-ops when oldLabel has no entry (block was open)", () => {
    act(() => {
      useNodeCollapseStore.getState().renameBlock("wf-1", "Missing", "Renamed");
    });

    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-1", "Renamed")
      ],
    ).toBeUndefined();
    expect(Object.keys(useNodeCollapseStore.getState().collapsed)).toHaveLength(
      0,
    );
  });

  test("does not cross workflow boundaries", () => {
    act(() => {
      useNodeCollapseStore.getState().toggleBlock("wf-1", "Shared Label");
      useNodeCollapseStore.getState().toggleBlock("wf-2", "Shared Label");
    });
    act(() => {
      useNodeCollapseStore
        .getState()
        .renameBlock("wf-1", "Shared Label", "Renamed");
    });

    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-1", "Shared Label")
      ],
    ).toBeUndefined();
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-1", "Renamed")
      ],
    ).toBe(true);
    // wf-2 entry untouched.
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-2", "Shared Label")
      ],
    ).toBe(true);
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-2", "Renamed")
      ],
    ).toBeUndefined();
  });
});
