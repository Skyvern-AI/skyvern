// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { GoogleOAuthCredential } from "@/api/types";

import { useResolveDefaultGoogleSheetsCredential } from "./useResolveDefaultGoogleSheetsCredential";

const updateNodeData = vi.fn();
const setHasChanges = vi.fn();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({ updateNodeData }),
  };
});

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: (selector: (s: unknown) => unknown) =>
    selector({ setHasChanges }),
}));

let mockCredentials: GoogleOAuthCredential[] = [];
let mockIsLoading = false;
let mockIsFetching = false;

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

function credential(id: string, valid: boolean = true): GoogleOAuthCredential {
  return {
    id,
    organization_id: "o_1",
    credential_name: id,
    scopes: null,
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
  useResolveDefaultGoogleSheetsCredential(nodes, readOnly);
  return null;
}

beforeEach(() => {
  updateNodeData.mockReset();
  setHasChanges.mockReset();
  mockCredentials = [];
  mockIsLoading = false;
  mockIsFetching = false;
});

afterEach(() => {
  cleanup();
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

  test("prefers the first valid credential over an invalid one", () => {
    mockCredentials = [
      credential("cred_invalid", false),
      credential("cred_valid", true),
    ];
    render(<Harness nodes={[writeNode("g1", "")]} />);

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      credentialId: "cred_valid",
    });
  });

  test("falls back to the only (invalid) credential rather than leaving it blank", () => {
    mockCredentials = [credential("cred_only", false)];
    render(<Harness nodes={[writeNode("g1", "")]} />);

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      credentialId: "cred_only",
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
