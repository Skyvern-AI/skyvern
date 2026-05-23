// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { LoginNode } from "../../nodes/LoginNode/types";

// Heavy subcomponents pull in network/data layers (workflow params store,
// credentials query, etc.) that are out of scope for a unit test of the
// form's own composition. Replace each with a typed stub that surfaces the
// props the form passes so we can assert wiring without spinning up the
// full editor context.
vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: (props: {
    nodeId?: string;
    value: string;
    placeholder?: string;
    onChange: (value: string) => void;
  }) => (
    <textarea
      data-testid="wbi-textarea"
      data-placeholder={props.placeholder ?? ""}
      data-node-id={props.nodeId ?? ""}
      value={props.value}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/components/ModelSelector", () => ({
  ModelSelector: (props: { value: unknown }) => (
    <div
      data-testid="model-selector"
      data-value={props.value === null ? "null" : "set"}
    />
  ),
}));

vi.mock("@/hooks/useRerender", () => ({
  useRerender: () => ({
    key: "test-rerender-key",
    bump: vi.fn(),
  }),
}));
vi.mock("@/components/EngineSelector", () => ({
  RunEngineSelector: (props: { value: unknown }) => (
    <div
      data-testid="engine-selector"
      data-value={props.value === null ? "null" : String(props.value)}
    />
  ),
}));

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: () => <span data-testid="help-tooltip" />,
}));

vi.mock("../../nodes/LoginNode/LoginBlockCredentialSelector", () => ({
  LoginBlockCredentialSelector: (props: {
    nodeId: string;
    value: string | undefined;
    currentUrl: string;
  }) => (
    <div
      data-testid="login-credential-selector"
      data-node-id={props.nodeId}
      data-value={props.value ?? ""}
      data-current-url={props.currentUrl}
    />
  ),
}));

vi.mock("../../nodes/TaskNode/ParametersMultiSelect", () => ({
  ParametersMultiSelect: (props: { parameters: Array<string> }) => (
    <div
      data-testid="parameters-multi-select"
      data-count={props.parameters.length}
    />
  ),
}));

vi.mock("../../nodes/components/BlockExecutionOptions", () => ({
  BlockExecutionOptions: (props: {
    continueOnFailure: boolean;
    blockType: string;
  }) => (
    <div
      data-testid="block-execution-options"
      data-continue={String(props.continueOnFailure)}
      data-block-type={props.blockType}
    />
  ),
}));

vi.mock("../../nodes/DisableCache", () => ({
  DisableCache: (props: { disableCache: boolean }) => (
    <div
      data-testid="disable-cache"
      data-disabled={String(props.disableCache)}
    />
  ),
}));

vi.mock("../../ErrorCodeMappingEditor", () => ({
  ErrorCodeMappingEditor: (props: { value: string }) => (
    <div data-testid="error-code-mapping-editor" data-value={props.value} />
  ),
}));

vi.mock("../../hooks/useIsFirstNodeInWorkflow", () => ({
  useIsFirstBlockInWorkflow: () => false,
}));

vi.mock("../../workflowEditorUtils", () => ({
  getAvailableOutputParameterKeys: () => [],
  isNodeInsideForLoop: () => false,
  getParentLoopSkipsOnFail: () => false,
}));

// Drive the dispatcher's `useReactFlow().getNode(blockId)` lookup via a
// mutable map so each test sets the fixture for its blockId. Same pattern
// as BlockConfigForm.test.tsx (SKY-9361).
const mockNodeFixtures = new Map<
  string,
  { id: string; type: string; data?: Record<string, unknown> } | undefined
>();
const updateNodeDataMock = vi.fn();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => mockNodeFixtures.get(id),
      updateNodeData: updateNodeDataMock,
    }),
    // LoginEditor (nested under the form) subscribes via useNodesData;
    // mirror getNode's stub here so the fixture covers both reads.
    useNodesData: (id: string) => {
      const node = mockNodeFixtures.get(id);
      if (!node) return null;
      return { id: node.id, type: node.type, data: node.data };
    },
    useNodes: () => Array.from(mockNodeFixtures.values()),
    useEdges: () => [],
  };
});

type DebouncedOpts = {
  blockId: string;
  value: unknown;
};
const useDebouncedSidebarSaveMock = vi.fn();
vi.mock("../useDebouncedSidebarSave", () => ({
  useDebouncedSidebarSave: (opts: DebouncedOpts) => {
    useDebouncedSidebarSaveMock(opts);
    return mockHookReturn;
  },
}));

let mockHookReturn: {
  commit: () => boolean;
};

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { LoginBlockForm } from "./LoginBlockForm";

function makeLoginFixture(
  id: string,
  overrides: Partial<LoginNode["data"]> = {},
): { id: string; type: "login"; data: LoginNode["data"] } {
  const data: LoginNode["data"] = {
    debuggable: true,
    label: "Login",
    url: "",
    navigationGoal: "log in to the site",
    errorCodeMapping: "null",
    maxRetries: null,
    maxStepsOverride: null,
    editable: true,
    parameterKeys: [],
    totpVerificationUrl: null,
    totpIdentifier: null,
    continueOnFailure: false,
    disableCache: false,
    completeCriterion: "",
    terminateCriterion: "",
    engine: null,
    model: null,
    ...overrides,
  };
  return { id, type: "login", data };
}

beforeEach(() => {
  mockNodeFixtures.clear();
  usePendingCommitsStore.setState({ commits: {} });
  updateNodeDataMock.mockReset();
  useDebouncedSidebarSaveMock.mockReset();
  mockHookReturn = {
    commit: vi.fn(() => true),
  };
});

afterEach(() => {
  cleanup();
});

describe("LoginBlockForm (SKY-9374)", () => {
  test("returns null when the node lookup misses (block was deleted)", () => {
    const { container } = render(<LoginBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null when the node is not a login block", () => {
    mockNodeFixtures.set("b1", { id: "b1", type: "task" });
    const { container } = render(<LoginBlockForm blockId="b1" />);
    expect(container.firstChild).toBeNull();
    expect(useDebouncedSidebarSaveMock).not.toHaveBeenCalled();
  });

  test("renders the basic fields and the advanced-settings accordion trigger", () => {
    mockNodeFixtures.set("b1", makeLoginFixture("b1"));
    render(<LoginBlockForm blockId="b1" />);

    // Outside the (collapsed) Advanced Settings accordion: URL + Login
    // Goal textareas, plus the Authentication group (Credential selector +
    // 2FA Identifier + 2FA Verification URL textareas). Radix
    // AccordionContent does not render its children while closed, so
    // these are the only basic-section assertions we can make without
    // expanding it.
    expect(screen.getAllByTestId("wbi-textarea")).toHaveLength(4);
    expect(screen.getByTestId("login-credential-selector")).toBeDefined();
    expect(screen.getByText("Advanced Settings")).toBeDefined();
  });

  test("expanding Advanced Settings reveals every inline-form field", () => {
    mockNodeFixtures.set("b1", makeLoginFixture("b1"));
    render(<LoginBlockForm blockId="b1" />);

    // Radix AccordionContent unmounts while collapsed, so reach the
    // advanced-settings widgets by clicking the trigger first. After
    // expansion every field the inline LoginNode form owns must be
    // present — pin the count so additions/removals force this test to
    // be updated explicitly instead of silently drifting parity.
    fireEvent.click(screen.getByText("Advanced Settings"));

    expect(screen.getAllByTestId("wbi-textarea")).toHaveLength(5);
    expect(screen.getByTestId("parameters-multi-select")).toBeDefined();
    expect(screen.getByTestId("model-selector")).toBeDefined();
    expect(screen.getByTestId("engine-selector")).toBeDefined();
    expect(screen.getByTestId("block-execution-options")).toBeDefined();
    expect(screen.getByTestId("disable-cache")).toBeDefined();
    expect(screen.getByPlaceholderText("Default: 10")).toBeDefined();
    // Error-mapping editor only appears when the mapping is non-null —
    // default fixture is "null" so the editor stays hidden even after
    // the accordion is expanded.
    expect(screen.queryByTestId("error-code-mapping-editor")).toBeNull();
  });

  test("renders the error-code-mapping editor when mapping is non-null", () => {
    mockNodeFixtures.set(
      "b1",
      makeLoginFixture("b1", {
        errorCodeMapping: '{"sample":"value"}',
      }),
    );
    render(<LoginBlockForm blockId="b1" />);
    fireEvent.click(screen.getByText("Advanced Settings"));
    const editor = screen.getByTestId("error-code-mapping-editor");
    expect(editor.getAttribute("data-value")).toBe('{"sample":"value"}');
  });

  test("feeds blockId + value into useDebouncedSidebarSave", () => {
    mockNodeFixtures.set(
      "b1",
      makeLoginFixture("b1", {
        url: "https://example.test/login",
        navigationGoal: "do the thing",
      }),
    );
    render(<LoginBlockForm blockId="b1" />);

    expect(useDebouncedSidebarSaveMock).toHaveBeenCalled();
    const opts = useDebouncedSidebarSaveMock.mock.lastCall?.[0] as
      | DebouncedOpts
      | undefined;
    expect(opts).toBeDefined();
    expect(opts!.blockId).toBe("b1");
    expect(opts!.value).toMatchObject({
      url: "https://example.test/login",
      navigationGoal: "do the thing",
      errorCodeMapping: "null",
      parameterKeys: [],
      continueOnFailure: false,
      disableCache: false,
    });
  });

  test("typing into a field calls updateNodeData immediately", () => {
    mockNodeFixtures.set("b1", makeLoginFixture("b1", { url: "" }));
    render(<LoginBlockForm blockId="b1" />);

    const textareas = screen.getAllByTestId(
      "wbi-textarea",
    ) as HTMLTextAreaElement[];
    const urlTextarea = textareas[0]!;
    fireEvent.change(urlTextarea, {
      target: { value: "https://example.test/login" },
    });

    // Containerized editors write through useUpdate on every onChange so
    // the tile and sidebar surfaces stay in sync via React Flow node data.
    expect(updateNodeDataMock).toHaveBeenCalledWith("b1", {
      url: "https://example.test/login",
    });
  });

  test("mount registers the commit fn; unmount unregisters", () => {
    mockNodeFixtures.set("b1", makeLoginFixture("b1"));
    const { unmount } = render(<LoginBlockForm blockId="b1" />);

    const registered = usePendingCommitsStore.getState().commits["b1"];
    expect(registered).toBe(mockHookReturn.commit);

    unmount();
    expect(usePendingCommitsStore.getState().commits["b1"]).toBeUndefined();
  });
});
