// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { ProxyLocation } from "@/api/types";

import type {
  WorkflowBlock,
  WorkflowSettings,
} from "../../types/workflowTypes";
import { getElements } from "../workflowEditorUtils";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import { WorkflowDocEditor } from "./WorkflowDocEditor";

const output = (label: string) => ({
  parameter_type: "output",
  key: `${label}_output`,
  output_parameter_id: `op-${label}`,
});
const code = (label: string, next: string | null) =>
  ({
    label,
    block_type: "code",
    next_block_label: next,
    continue_on_failure: false,
    model: null,
    output_parameter: output(label),
    code: "",
    parameters: [],
  }) as unknown as WorkflowBlock;
const loop = {
  label: "each_row",
  block_type: "for_loop",
  next_block_label: null,
  continue_on_failure: false,
  model: null,
  output_parameter: output("each_row"),
  loop_over: { key: "items" },
  loop_blocks: [code("inner_step", null)],
  loop_variable_reference: "{{ items }}",
  complete_if_empty: false,
} as unknown as WorkflowBlock;

function renderDoc() {
  const { nodes, edges } = getElements(
    [loop],
    {
      proxyLocation: ProxyLocation.Residential,
      finallyBlockLabel: null,
    } as unknown as WorkflowSettings,
    true,
  );
  render(<WorkflowDocEditor nodes={nodes} edges={edges} />);
  return nodes;
}

describe("WorkflowDocEditor navigation", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => {
      cb();
      return 0;
    });
    vi.stubGlobal("CSS", { escape: (value: string) => value });
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => {
    cleanup();
    useLocateBlockStore.getState().clearLocate();
    vi.unstubAllGlobals();
  });

  test("Locate on a step inside a collapsed loop reopens the loop and consumes the request", () => {
    const nodes = renderDoc();
    const inner = nodes.find((n) => n.data.label === "inner_step")!;
    expect(
      document.querySelector(`[data-doc-node-id="${inner.id}"]`),
    ).not.toBeNull();

    const toggle = screen
      .getAllByRole("button")
      .find((b) => b.getAttribute("aria-expanded") === "true")!;
    fireEvent.click(toggle);
    expect(
      document.querySelector(`[data-doc-node-id="${inner.id}"]`),
    ).toBeNull();

    act(() => useLocateBlockStore.getState().requestLocate(inner.id));

    expect(
      document.querySelector(`[data-doc-node-id="${inner.id}"]`),
    ).not.toBeNull();
    expect(useLocateBlockStore.getState().request).toBeNull();
  });
});
