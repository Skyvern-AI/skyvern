// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import type { NodeProps } from "@xyflow/react";
import { type ComponentType } from "react";

import { withRunValidationHighlight } from "./withRunValidationHighlight";
import { useRunValidationStore } from "./useRunValidationStore";

function NodeBody() {
  return <div data-testid="node-body">node</div>;
}

const Wrapped = withRunValidationHighlight(
  NodeBody as ComponentType<NodeProps>,
);

function nodeProps(id: string, label: string): NodeProps {
  return { id, data: { label } } as unknown as NodeProps;
}

const BADGE_LABEL = /needs a credential/i;

describe("withRunValidationHighlight", () => {
  beforeEach(() => {
    useRunValidationStore.getState().setBlockingBlocks([]);
  });
  afterEach(() => {
    cleanup();
    useRunValidationStore.getState().setBlockingBlocks([]);
  });

  test("flags a block whose node id is run-blocking", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([{ id: "n1", label: "block_2" }]);
    const { container } = render(<Wrapped {...nodeProps("n1", "block_2")} />);
    expect(screen.getByLabelText(BADGE_LABEL)).toBeTruthy();
    expect(container.querySelector('[data-run-blocking="true"]')).toBeTruthy();
  });

  test("does not flag a healthy block", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([{ id: "n1", label: "block_2" }]);
    const { container } = render(<Wrapped {...nodeProps("n2", "block_1")} />);
    expect(screen.queryByLabelText(BADGE_LABEL)).toBeNull();
    expect(container.querySelector('[data-run-blocking="true"]')).toBeNull();
  });

  test("matches on node id, not label", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([{ id: "n1", label: "block_2" }]);
    const { container } = render(<Wrapped {...nodeProps("n9", "block_2")} />);
    expect(container.querySelector('[data-run-blocking="true"]')).toBeNull();
  });
});
