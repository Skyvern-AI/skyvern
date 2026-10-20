// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";

import { terminateNodeDefaultData } from "../../nodes/TerminateNode/types";
import { TerminateBlockForm } from "./TerminateBlockForm";

const node = {
  id: "terminate-1",
  type: "terminate",
  data: { ...terminateNodeDefaultData, reason: "Stop here" },
};

vi.mock("../../nodes", () => ({
  isWorkflowBlockNode: () => true,
}));

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: () => node,
      updateNodeData: (_id: string, updates: object) => {
        node.data = { ...node.data, ...updates };
      },
    }),
    useNodesData: () => node,
  };
});

vi.mock("@/components/WorkflowBlockInput", () => ({
  WorkflowBlockInput: ({
    name,
    value,
    placeholder,
    onChange,
  }: {
    name: string;
    value: string;
    placeholder: string;
    onChange: (value: string) => void;
  }) => (
    <input
      name={name}
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: () => <textarea />,
}));

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: () => null,
}));

beforeEach(() => {
  node.data = { ...terminateNodeDefaultData, reason: "Stop here" };
  usePendingCommitsStore.setState({ commits: {} });
  useSidebarSaveStateStore.getState().reset();
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("Terminate editor updates node data when its Error Code input changes", () => {
  render(<TerminateBlockForm blockId={node.id} />);

  fireEvent.change(screen.getByPlaceholderText("e.g. ACCOUNT_NOT_FOUND"), {
    target: { value: "ACCOUNT_NOT_FOUND" },
  });

  expect(node.data.errorCode).toBe("ACCOUNT_NOT_FOUND");
});

test("Terminate sidebar pending commit tracks an Error Code-only edit", () => {
  const { rerender } = render(<TerminateBlockForm blockId={node.id} />);

  fireEvent.change(screen.getByPlaceholderText("e.g. ACCOUNT_NOT_FOUND"), {
    target: { value: "ACCOUNT_NOT_FOUND" },
  });
  rerender(<TerminateBlockForm blockId={node.id} />);

  expect(
    useSidebarSaveStateStore.getState().getLastUpdatedAt(node.id),
  ).toBeNull();
  act(() => {
    expect(usePendingCommitsStore.getState().flush(node.id)).toBe(true);
  });
  expect(useSidebarSaveStateStore.getState().getLastUpdatedAt(node.id)).toBe(
    Date.now(),
  );
});
