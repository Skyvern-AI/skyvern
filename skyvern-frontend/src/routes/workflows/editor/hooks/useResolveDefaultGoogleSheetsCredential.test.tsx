// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  clearDeferredEdits,
  deferredEdits,
} from "@/hooks/useDeferredLockedEdit";
import { WorkflowScopeContext } from "../WorkflowScopeContext";

import type { GoogleOAuthCredential } from "@/api/types";
import { ReactFlowProvider, useNodes, useReactFlow } from "@xyflow/react";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import {
  beginCopilotAcceptance,
  beginSaveTransaction,
  beginYamlCommit,
  createYamlCommitOwner,
  finishCopilotAcceptance,
  finishSaveTransaction,
  finishYamlCommit,
  registerEditorOwner,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import {
  googleSheetsReadNodeDefaultData,
  type GoogleSheetsReadNode,
} from "../nodes/GoogleSheetsReadNode/types";
import {
  googleSheetsWriteNodeDefaultData,
  type GoogleSheetsWriteNode,
} from "../nodes/GoogleSheetsWriteNode/types";

import { useResolveDefaultGoogleSheetsCredential } from "./useResolveDefaultGoogleSheetsCredential";

const updateNodeData = vi.fn();
const getNode = vi.fn();
const setHasChanges = vi.fn(
  useWorkflowHasChangesStore.getInitialState().setHasChanges,
);
let useRealReactFlow = false;

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () =>
      useRealReactFlow ? actual.useReactFlow() : { getNode, updateNodeData },
  };
});

let mockCredentials: GoogleOAuthCredential[] = [];
let mockIsLoading = false;
let mockIsFetching = false;
const GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets";
const GOOGLE_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file";
const GOOGLE_DRIVE_METADATA_SCOPE =
  "https://www.googleapis.com/auth/drive.metadata.readonly";
const GOOGLE_SHEETS_REQUIRED_SCOPES = [
  GOOGLE_SHEETS_SCOPE,
  GOOGLE_DRIVE_FILE_SCOPE,
  GOOGLE_DRIVE_METADATA_SCOPE,
];
const GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly";

vi.mock("@/hooks/useGoogleOAuthCredentials", async () => {
  const actual = await vi.importActual<
    typeof import("@/hooks/useGoogleOAuthCredentials")
  >("@/hooks/useGoogleOAuthCredentials");
  return {
    ...actual,
    useGoogleOAuthCredentials: () => ({
      credentials: mockCredentials,
      isLoading: mockIsLoading,
      isFetching: mockIsFetching,
    }),
  };
});

function credential(
  id: string,
  state: string = "active",
  scopesGranted: string[] = GOOGLE_SHEETS_REQUIRED_SCOPES,
): GoogleOAuthCredential {
  return {
    id,
    organization_id: "o_1",
    credential_name: id,
    provider: "google",
    state,
    scopes_requested: scopesGranted,
    scopes_granted: scopesGranted,
    created_at: "",
    modified_at: "",
  };
}

function legacyCredential(
  id: string,
  valid: boolean = true,
  scopes: string[] = GOOGLE_SHEETS_REQUIRED_SCOPES,
): GoogleOAuthCredential {
  return {
    id,
    organization_id: "o_1",
    credential_name: id,
    scopes,
    valid,
    created_at: "",
    modified_at: "",
  };
}

// Minimal node shapes; only `type`, `id`, and `data.{editable,credentialId}`
// are read by the hook + the real node type guards.
function writeNode(id: string, credentialId: string, editable = true) {
  return {
    id,
    type: "googleSheetsWrite",
    data: { editable, credentialId },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any;
}

function readNode(id: string, credentialId: string, editable = true) {
  return {
    id,
    type: "googleSheetsRead",
    data: { editable, credentialId },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any;
}

function taskNode(id: string) {
  return {
    id,
    type: "task",
    data: { editable: true },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any;
}

function Harness({
  nodes,
  readOnly = false,
}: {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  nodes: any[];
  readOnly?: boolean;
}) {
  getNode.mockImplementation((id: string) =>
    nodes.find((node) => node.id === id),
  );
  useResolveDefaultGoogleSheetsCredential(nodes, readOnly);
  return null;
}

beforeEach(() => {
  updateNodeData.mockReset();
  getNode.mockReset();
  setHasChanges.mockClear();
  useRealReactFlow = false;
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
    true,
  );
  useWorkflowHasChangesStore.setState(
    {
      ...useWorkflowHasChangesStore.getInitialState(),
      setHasChanges,
    },
    true,
  );
  mockCredentials = [];
  mockIsLoading = false;
  mockIsFetching = false;
});

afterEach(() => {
  cleanup();
  clearDeferredEdits();
  vi.useRealTimers();
});

describe("useResolveDefaultGoogleSheetsCredential (SKY-11219)", () => {
  test("fills the default account into a write block with no credential", () => {
    mockCredentials = [credential("cred_default")];
    render(<Harness nodes={[writeNode("g1", "")]} />);

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      credentialId: "cred_default",
    });
  });

  // Deferred past the synchronous effect flush so it lands after Workspace's
  // mount initializer resets the flag (setHasChanges(false)).
  test("marks the workflow dirty (deferred) so the fill is persisted", async () => {
    mockCredentials = [credential("cred_default")];
    render(<Harness nodes={[writeNode("g1", "")]} />);

    expect(setHasChanges).not.toHaveBeenCalled();
    await Promise.resolve();
    expect(setHasChanges).toHaveBeenCalledWith(true);
  });

  test("does not mark dirty when nothing needs filling", async () => {
    mockCredentials = [credential("cred_default")];
    render(<Harness nodes={[writeNode("g1", "cred_existing")]} />);

    await Promise.resolve();
    expect(setHasChanges).not.toHaveBeenCalled();
  });

  test("does not fill while a credentials refetch is in flight", () => {
    mockCredentials = [credential("cred_default")];
    mockIsFetching = true;
    render(<Harness nodes={[writeNode("g1", "")]} />);

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("fills read blocks too", () => {
    mockCredentials = [credential("cred_default")];
    render(<Harness nodes={[readNode("r1", "")]} />);

    expect(updateNodeData).toHaveBeenCalledWith("r1", {
      credentialId: "cred_default",
    });
  });

  test("fills from the legacy credential response shape during split deploys", () => {
    mockCredentials = [legacyCredential("cred_legacy")];
    render(<Harness nodes={[writeNode("g1", "")]} />);

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      credentialId: "cred_legacy",
    });
  });

  test("prefers the first valid credential over an invalid one", () => {
    mockCredentials = [
      credential("cred_invalid", "revoked"),
      credential("cred_valid", "active"),
    ];
    render(<Harness nodes={[writeNode("g1", "")]} />);

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      credentialId: "cred_valid",
    });
  });

  test("falls back to the only (invalid) credential rather than leaving it blank", () => {
    mockCredentials = [credential("cred_only", "revoked")];
    render(<Harness nodes={[writeNode("g1", "")]} />);

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      credentialId: "cred_only",
    });
  });

  test("ignores Gmail-only credentials when filling Sheets blocks", () => {
    mockCredentials = [
      credential("cred_gmail", "active", [GMAIL_SCOPE]),
      credential("cred_sheets"),
    ];
    render(<Harness nodes={[writeNode("g1", "")]} />);

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      credentialId: "cred_sheets",
    });
  });

  test("fills a grant that carries only the Sheets data scope, as the copilot reads it", () => {
    mockCredentials = [
      credential("cred_spreadsheets_only", "active", [GOOGLE_SHEETS_SCOPE]),
    ];
    render(<Harness nodes={[writeNode("g1", "")]} />);

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      credentialId: "cred_spreadsheets_only",
    });
  });

  test("leaves an already-configured credential untouched", () => {
    mockCredentials = [credential("cred_default")];
    render(<Harness nodes={[writeNode("g1", "cred_existing")]} />);

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("does nothing when no Google account is connected", () => {
    mockCredentials = [];
    render(<Harness nodes={[writeNode("g1", "")]} />);

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("does not fill while credentials are still loading", () => {
    mockCredentials = [credential("cred_default")];
    mockIsLoading = true;
    render(<Harness nodes={[writeNode("g1", "")]} />);

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("does not fill in read-only canvases", () => {
    mockCredentials = [credential("cred_default")];
    render(<Harness nodes={[writeNode("g1", "")]} readOnly />);

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("does not fill non-editable blocks", () => {
    mockCredentials = [credential("cred_default")];
    render(<Harness nodes={[writeNode("g1", "", false)]} />);

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("ignores non-Google-Sheets blocks", () => {
    mockCredentials = [credential("cred_default")];
    render(<Harness nodes={[taskNode("t1")]} />);

    expect(updateNodeData).not.toHaveBeenCalled();
  });
});

function DeferredSheetsInput() {
  const nodes = useNodes<GoogleSheetsReadNode | GoogleSheetsWriteNode>();
  const { updateNodeData } = useReactFlow<
    GoogleSheetsReadNode | GoogleSheetsWriteNode
  >();
  return (
    <WorkflowBlockInputTextarea
      name="credentialId:google"
      nodeId="g1"
      value={nodes[0]?.data.credentialId ?? ""}
      onChange={(credentialId) => updateNodeData("g1", { credentialId })}
      hideActions
    />
  );
}

function DeferredSheetsHarness({ autoFillFirst }: { autoFillFirst: boolean }) {
  const input = <DeferredSheetsInput key="input" />;
  const autoFill = <LiveHarness key="auto-fill" />;
  return (
    <WorkflowScopeContext.Provider
      value={{ workflowId: "workflow-test", readOnly: false }}
    >
      {autoFillFirst ? [autoFill, input] : [input, autoFill]}
    </WorkflowScopeContext.Provider>
  );
}

function LiveHarness() {
  const nodes = useNodes<GoogleSheetsReadNode | GoogleSheetsWriteNode>();
  const hasChanges = useWorkflowHasChangesStore((state) => state.hasChanges);
  useResolveDefaultGoogleSheetsCredential(nodes, false, "workflow-test");
  return (
    <>
      <output data-testid="credential">{nodes[0]?.data.credentialId}</output>
      <output data-testid="dirty">{String(hasChanges)}</output>
    </>
  );
}

describe("default Sheets credentials during editor locks", () => {
  test.each([
    ["save", "googleSheetsRead", false],
    ["save", "googleSheetsRead", true],
    ["copilot", "googleSheetsRead", false],
    ["copilot", "googleSheetsRead", true],
    ["save", "googleSheetsWrite", false],
    ["save", "googleSheetsWrite", true],
    ["copilot", "googleSheetsWrite", false],
    ["copilot", "googleSheetsWrite", true],
  ] as const)(
    "preserves a buffered %s template in %s (auto-fill first: %s)",
    async (lock, type, autoFillFirst) => {
      vi.useFakeTimers();
      useRealReactFlow = true;
      mockIsFetching = true;
      const nodes: Array<GoogleSheetsReadNode | GoogleSheetsWriteNode> = [
        type === "googleSheetsRead"
          ? {
              id: "g1",
              type,
              position: { x: 0, y: 0 },
              data: { ...googleSheetsReadNodeDefaultData, credentialId: "" },
            }
          : {
              id: "g1",
              type,
              position: { x: 0, y: 0 },
              data: { ...googleSheetsWriteNodeDefaultData, credentialId: "" },
            },
      ];
      const harness = (
        <ReactFlowProvider defaultNodes={nodes}>
          <DeferredSheetsHarness autoFillFirst={autoFillFirst} />
        </ReactFlowProvider>
      );
      const view = render(harness);
      const userValue = "{{ chosen_credential }}";
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: userValue },
      });
      let releaseLock: () => void;
      act(() => {
        if (lock === "save") {
          const owner = createYamlCommitOwner("workflow-test");
          registerEditorOwner(owner);
          expect(beginSaveTransaction(owner)).toBe(true);
          releaseLock = () => finishSaveTransaction(owner);
        } else {
          const token = beginCopilotAcceptance();
          expect(token).not.toBeNull();
          releaseLock = () => finishCopilotAcceptance(token!);
        }
      });
      const deferKey = JSON.stringify([
        "workflow-test",
        "g1",
        "credentialId:google",
      ]);
      expect(deferredEdits.get(deferKey)?.value).toBe(userValue);
      mockCredentials = [credential("cred_default")];
      mockIsFetching = false;
      view.rerender(
        <ReactFlowProvider defaultNodes={nodes}>
          <DeferredSheetsHarness autoFillFirst={autoFillFirst} />
        </ReactFlowProvider>,
      );
      act(() => vi.advanceTimersByTime(300));
      expect(screen.getByTestId("credential").textContent).toBe("");

      await act(async () => releaseLock());
      act(() => vi.advanceTimersByTime(300));

      expect(screen.getByTestId("credential").textContent).toBe(userValue);
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
        userValue,
      );
      expect(deferredEdits.has(deferKey)).toBe(false);
      expect(setHasChanges).not.toHaveBeenCalled();
    },
  );

  test.each(["save", "yaml", "copilot"] as const)(
    "fills and marks dirty after the %s lock releases",
    async (lock) => {
      useRealReactFlow = true;
      mockIsLoading = true;
      mockIsFetching = true;
      const nodes: Array<GoogleSheetsReadNode | GoogleSheetsWriteNode> = [
        lock === "save"
          ? {
              id: "g1",
              type: "googleSheetsWrite",
              position: { x: 0, y: 0 },
              data: { ...googleSheetsWriteNodeDefaultData, credentialId: "" },
            }
          : {
              id: "g1",
              type: "googleSheetsRead",
              position: { x: 0, y: 0 },
              data: { ...googleSheetsReadNodeDefaultData, credentialId: "" },
            },
      ];
      const view = render(
        <ReactFlowProvider defaultNodes={nodes}>
          <LiveHarness />
        </ReactFlowProvider>,
      );
      let releaseLock: () => void;
      act(() => {
        if (lock === "save") {
          const owner = createYamlCommitOwner("wpid_test");
          registerEditorOwner(owner);
          expect(beginSaveTransaction(owner)).toBe(true);
          releaseLock = () => finishSaveTransaction(owner);
        } else if (lock === "yaml") {
          const owner = createYamlCommitOwner("wpid_test");
          expect(beginYamlCommit(owner)).toBe(true);
          releaseLock = () => finishYamlCommit(owner);
        } else {
          const token = beginCopilotAcceptance();
          expect(token).not.toBeNull();
          releaseLock = () => finishCopilotAcceptance(token!);
        }
      });

      mockCredentials = [credential("cred_default")];
      mockIsLoading = false;
      mockIsFetching = false;
      view.rerender(
        <ReactFlowProvider defaultNodes={nodes}>
          <LiveHarness />
        </ReactFlowProvider>,
      );
      await act(async () => {});
      expect(screen.getByTestId("credential").textContent).toBe("");
      expect(screen.getByTestId("dirty").textContent).toBe("false");
      expect(setHasChanges).not.toHaveBeenCalled();

      await act(async () => releaseLock());
      await waitFor(() => {
        expect(screen.getByTestId("credential").textContent).toBe(
          "cred_default",
        );
        expect(screen.getByTestId("dirty").textContent).toBe("true");
      });
    },
  );
});
