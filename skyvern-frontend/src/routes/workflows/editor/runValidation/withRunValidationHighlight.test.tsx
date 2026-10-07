// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import type { NodeProps } from "@xyflow/react";
import { type ComponentType } from "react";

import type { RunBlockingBlock } from "./getRunBlockingBlocks";
import { useLocateBlockStore } from "./useLocateBlockStore";
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

function block(
  id: string,
  label: string,
  ancestorIds: Array<string> = [],
): RunBlockingBlock {
  return { id, label, ancestorIds, path: [] };
}

const BADGE_LABEL = /needs a credential/i;

describe("withRunValidationHighlight", () => {
  beforeEach(() => {
    useRunValidationStore.getState().setBlockingBlocks([]);
    useLocateBlockStore.getState().clearPulse();
  });
  afterEach(() => {
    cleanup();
    useRunValidationStore.getState().setBlockingBlocks([]);
    useLocateBlockStore.getState().clearPulse();
  });

  test("flags a block whose node id is run-blocking", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([block("n1", "block_2")]);
    const { container } = render(<Wrapped {...nodeProps("n1", "block_2")} />);
    expect(screen.getByLabelText(BADGE_LABEL)).toBeTruthy();
    expect(container.querySelector('[data-run-blocking="true"]')).toBeTruthy();
  });

  test("does not flag a healthy block", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([block("n1", "block_2")]);
    const { container } = render(<Wrapped {...nodeProps("n2", "block_1")} />);
    expect(screen.queryByLabelText(BADGE_LABEL)).toBeNull();
    expect(container.querySelector('[data-run-blocking="true"]')).toBeNull();
  });

  test("matches on node id, not label", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([block("n1", "block_2")]);
    const { container } = render(<Wrapped {...nodeProps("n9", "block_2")} />);
    expect(container.querySelector('[data-run-blocking="true"]')).toBeNull();
  });

  test("shows a rolled-up count on a container with a blocking descendant", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([block("n1", "block_2", ["loop1"])]);
    const { container } = render(<Wrapped {...nodeProps("loop1", "loop_1")} />);
    // The container itself is not the offender, so no full-outline flag...
    expect(container.querySelector('[data-run-blocking="true"]')).toBeNull();
    // ...but it carries a rolled-up count badge.
    expect(
      container.querySelector('[data-run-blocking-rollup="true"]'),
    ).toBeTruthy();
    expect(screen.getByText("1")).toBeTruthy();
  });

  test("pulses the located block", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([block("n1", "block_2")]);
    useLocateBlockStore.getState().startPulse("n1");
    const { container } = render(<Wrapped {...nodeProps("n1", "block_2")} />);
    expect(
      (container.firstChild as HTMLElement).className.includes(
        "animate-run-blocking-locate",
      ),
    ).toBe(true);
  });
});
