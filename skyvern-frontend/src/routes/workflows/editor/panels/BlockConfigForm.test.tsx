// @vitest-environment jsdom

import { act, cleanup, render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const mockNodeFixtures = new Map<
  string,
  { id: string; type: string; data?: Record<string, unknown> } | undefined
>();
vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useNodesData: (id: string) => mockNodeFixtures.get(id),
    useReactFlow: () => ({
      getNode: (id: string) => mockNodeFixtures.get(id),
    }),
  };
});

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({ value }: { value: string }) => (
    <textarea readOnly value={value} />
  ),
}));

// The sidebar header now renders an editable title for block nodes, which
// pulls in the label-change hook (needs a ReactFlow store). Stub it so these
// commit-orchestration tests stay scoped to the body, not the title. The
// title also needs ResizeObserver (absent in jsdom); that is stubbed per-test
// in beforeEach/afterEach below to avoid leaking the global across files.
vi.mock("@/routes/workflows/hooks/useLabelChangeHandler", () => ({
  useNodeLabelChangeHandler: ({
    initialValue,
  }: {
    id: string;
    initialValue: string;
  }) => [initialValue, () => {}] as const,
}));

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { BlockConfigSidebar } from "./BlockConfigSidebar";
import {
  BLOCK_FORMS,
  BlockConfigForm,
  type BlockFormComponent,
  type WorkflowBlockNodeType,
} from "./BlockConfigForm";

const BLOCK_FORM_KEYS = Object.keys(BLOCK_FORMS) as WorkflowBlockNodeType[];
const ORIGINAL_BLOCK_FORMS = { ...BLOCK_FORMS };

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  mockNodeFixtures.clear();
  usePendingCommitsStore.setState({ commits: {} });
  useWorkflowPanelStore.getState().setSelectedBlockId(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
  for (const key of BLOCK_FORM_KEYS) {
    BLOCK_FORMS[key] = ORIGINAL_BLOCK_FORMS[key];
  }
});

describe("BLOCK_FORMS dispatcher", () => {
  test("every block type maps to a renderable component (no null entries)", () => {
    for (const [type, form] of Object.entries(BLOCK_FORMS)) {
      expect(form, `BLOCK_FORMS.${type} must be a component`).toBeDefined();
      expect(form, `BLOCK_FORMS.${type} must not be null`).not.toBeNull();
    }
  });

  test("contains the 27 expected node.type keys", () => {
    expect(new Set(BLOCK_FORM_KEYS)).toEqual(
      new Set([
        "task",
        "taskv2",
        "navigation",
        "extraction",
        "action",
        "login",
        "wait",
        "loop",
        "conditional",
        "textPrompt",
        "sendEmail",
        "codeBlock",
        "fileParser",
        "fileDownload",
        "download",
        "upload",
        "fileUpload",
        "pdfParser",
        "validation",
        "human_interaction",
        "url",
        "http_request",
        "printPage",
        "workflowTrigger",
        "googleSheetsRead",
        "googleSheetsWrite",
        "pdfFill",
      ]),
    );
    expect(BLOCK_FORM_KEYS).toHaveLength(27);
  });

  test("conditional routes to a sidebar form that shows branch prompts", () => {
    mockNodeFixtures.set("c1", {
      id: "c1",
      type: "conditional",
      data: {
        editable: true,
        branches: [
          {
            id: "branch_a",
            criteria: {
              criteria_type: "jinja2_template",
              expression: "{{ total > 100 }}",
              description: null,
            },
            next_block_label: null,
            description: null,
            is_default: false,
          },
          {
            id: "branch_default",
            criteria: null,
            next_block_label: null,
            description: null,
            is_default: true,
          },
        ],
        activeBranchId: "branch_a",
        mergeLabel: null,
        continueOnFailure: false,
        nextLoopOnFailure: false,
      },
    });
    render(<BlockConfigForm blockId="c1" />);
    expect(screen.getByTestId("conditional-block-form")).toBeDefined();
    expect(screen.getByDisplayValue("{{ total > 100 }}")).toBeDefined();
  });

  test("returns null when the node lookup misses (block was deleted)", () => {
    mockNodeFixtures.set("missing", undefined);
    const { container } = render(<BlockConfigForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for the NodeAdder utility node", () => {
    mockNodeFixtures.set("u1", { id: "u1", type: "nodeAdder" });

    const { container } = render(<BlockConfigForm blockId="u1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders the registered form for the looked-up node type", () => {
    const StubTaskForm: BlockFormComponent = ({ blockId }) => (
      <div data-testid="stub-task-form" data-block-id={blockId}>
        stub task form
      </div>
    );
    BLOCK_FORMS.task = StubTaskForm;
    mockNodeFixtures.set("b1", { id: "b1", type: "task" });

    render(<BlockConfigForm blockId="b1" />);

    const form = screen.getByTestId("stub-task-form");
    expect(form.getAttribute("data-block-id")).toBe("b1");
  });

  test("each block-type key resolves independently (no cross-talk between routes)", () => {
    const StubLoopForm: BlockFormComponent = () => (
      <div data-testid="stub-loop-form" />
    );
    const StubTaskForm: BlockFormComponent = () => (
      <div data-testid="stub-task-form" />
    );
    BLOCK_FORMS.loop = StubLoopForm;
    BLOCK_FORMS.task = StubTaskForm;

    mockNodeFixtures.set("loop-1", { id: "loop-1", type: "loop" });
    mockNodeFixtures.set("task-1", { id: "task-1", type: "task" });

    const { rerender } = render(<BlockConfigForm blockId="loop-1" />);
    expect(screen.getByTestId("stub-loop-form")).toBeDefined();
    expect(screen.queryByTestId("stub-task-form")).toBeNull();

    rerender(<BlockConfigForm blockId="task-1" />);
    expect(screen.getByTestId("stub-task-form")).toBeDefined();
    expect(screen.queryByTestId("stub-loop-form")).toBeNull();
  });
});

describe("BlockConfigForm switching-blocks commit orchestration", () => {
  test("flushes the previous block's registered commit when selectedBlockId changes", () => {
    const commitForA = vi.fn(() => true);

    const StubTaskForm: BlockFormComponent = ({ blockId }) => {
      const register = usePendingCommitsStore((state) => state.register);
      const unregister = usePendingCommitsStore((state) => state.unregister);
      useEffect(() => {
        register(blockId, commitForA);
        return () => {
          unregister(blockId);
        };
      }, [blockId, register, unregister]);
      return <div data-testid="stub-task-form" />;
    };
    BLOCK_FORMS.task = StubTaskForm;

    mockNodeFixtures.set("block-a", { id: "block-a", type: "task" });
    mockNodeFixtures.set("block-b", { id: "block-b", type: "task" });

    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );
    expect(commitForA).not.toHaveBeenCalled();

    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-b");
    });

    expect(commitForA).toHaveBeenCalledTimes(1);
  });

  test("does not flush anything when the sidebar opens for the first time", () => {
    BLOCK_FORMS.task = () => <div data-testid="stub-task-form" />;
    const commit = vi.fn(() => true);
    usePendingCommitsStore.getState().register("block-a", commit);

    mockNodeFixtures.set("block-a", { id: "block-a", type: "task" });

    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );
    expect(commit).not.toHaveBeenCalled();

    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });

    expect(commit).not.toHaveBeenCalled();
  });
});
