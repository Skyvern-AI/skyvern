// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const mockNodes = new Map<
  string,
  { id: string; type: string; data?: Record<string, unknown> } | undefined
>();
const updateNodeData = vi.fn();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => mockNodes.get(id),
      updateNodeData,
    }),
    useNodes: () => Array.from(mockNodes.values()),
    useEdges: () => [],
    useNodesData: (id: string) => {
      const node = mockNodes.get(id);
      return node ? { id: node.id, type: node.type, data: node.data } : null;
    },
  };
});

vi.mock("@/components/WorkflowBlockInput", () => ({
  WorkflowBlockInput: (props: {
    value: string;
    onChange: (value: string) => void;
    type?: string;
  }) => (
    <input
      data-testid={`wbi-${props.type ?? "text"}`}
      type={props.type ?? "text"}
      value={props.value ?? ""}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: (props: {
    value: string;
    onChange: (value: string) => void;
  }) => (
    <textarea
      data-testid="wbi-textarea"
      value={props.value ?? ""}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/routes/workflows/components/GoogleOAuthCredentialSelector", () => ({
  GoogleOAuthCredentialSelector: (props: {
    value: string;
    onChange: (value: string) => void;
  }) => (
    <input
      data-testid="google-oauth-selector"
      value={props.value ?? ""}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

// Stub the shadcn Select to a native <select> so we can fire change events
// directly. The form only consumes value + onValueChange.
vi.mock("@/components/ui/select", () => {
  type SelectProps = {
    value?: string;
    onValueChange?: (value: string) => void;
    disabled?: boolean;
    children?: ReactNode;
  };
  const Select = ({
    value,
    onValueChange,
    disabled,
    children,
  }: SelectProps) => (
    <select
      data-testid="storage-type-select"
      value={value}
      disabled={disabled}
      onChange={(e) => onValueChange?.(e.target.value)}
    >
      {children}
    </select>
  );
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  const SelectItem = ({
    value,
    children,
  }: {
    value: string;
    children?: ReactNode;
  }) => <option value={value}>{children}</option>;
  return {
    Select,
    SelectContent: Pass,
    SelectTrigger: Pass,
    SelectValue: Pass,
    SelectItem,
  };
});

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { FileUploadBlockForm } from "./FileUploadBlockForm";

beforeEach(() => {
  vi.useFakeTimers();
  mockNodes.clear();
  updateNodeData.mockReset();
  usePendingCommitsStore.setState({ commits: {} });
  useSidebarSaveStateStore.getState().reset();
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

function setFileUploadNode(
  id: string,
  overrides: Partial<{
    storageType: "s3" | "azure" | "google_drive";
    path: string;
    s3Bucket: string | null;
    awsAccessKeyId: string | null;
    awsSecretAccessKey: string | null;
    regionName: string | null;
    azureStorageAccountName: string | null;
    azureStorageAccountKey: string | null;
    azureBlobContainerName: string | null;
    googleCredentialId: string | null;
    googleDriveFolderId: string | null;
    editable: boolean;
  }> = {},
) {
  mockNodes.set(id, {
    id,
    type: "fileUpload",
    data: {
      storageType: overrides.storageType ?? "s3",
      path: overrides.path ?? "{{ workflow_run_id }}",
      s3Bucket: overrides.s3Bucket ?? null,
      awsAccessKeyId: overrides.awsAccessKeyId ?? null,
      awsSecretAccessKey: overrides.awsSecretAccessKey ?? null,
      regionName: overrides.regionName ?? null,
      azureStorageAccountName: overrides.azureStorageAccountName ?? null,
      azureStorageAccountKey: overrides.azureStorageAccountKey ?? null,
      azureBlobContainerName: overrides.azureBlobContainerName ?? null,
      googleCredentialId: overrides.googleCredentialId ?? null,
      googleDriveFolderId: overrides.googleDriveFolderId ?? null,
      editable: overrides.editable ?? true,
      label: "file_upload_1",
      continueOnFailure: false,
      debuggable: true,
      model: null,
    },
  });
}

describe("FileUploadBlockForm (SKY-9361)", () => {
  test("returns null for missing node", () => {
    const { container } = render(<FileUploadBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("f1", { id: "f1", type: "task", data: {} });
    const { container } = render(<FileUploadBlockForm blockId="f1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders s3 fields when storageType is 's3'", () => {
    setFileUploadNode("f1", {
      storageType: "s3",
      awsAccessKeyId: "AKIA...",
      awsSecretAccessKey: "secret",
      s3Bucket: "my-bucket",
      regionName: "us-west-2",
      path: "uploads",
    });
    render(<FileUploadBlockForm blockId="f1" />);

    expect(screen.getByText("Storage Type")).toBeDefined();
    expect(screen.getByText("AWS Access Key ID")).toBeDefined();
    expect(screen.getByText("AWS Secret Access Key")).toBeDefined();
    expect(screen.getByText("S3 Bucket")).toBeDefined();
    expect(screen.getByText("Region Name")).toBeDefined();
    expect(screen.getByText("(Optional) Folder Path")).toBeDefined();

    // Azure fields are not visible
    expect(screen.queryByText("Storage Account Name")).toBeNull();
    expect(screen.queryByText("Storage Account Key")).toBeNull();
    expect(screen.queryByText("Blob Container Name")).toBeNull();
    expect(screen.queryByText("Google Account")).toBeNull();
    expect(screen.queryByText("Google Drive Folder ID")).toBeNull();

    // 4 textareas (key id, bucket, region, path) and 1 password input
    const textareas = screen.getAllByTestId("wbi-textarea");
    expect(textareas).toHaveLength(4);
    const password = screen.getByTestId("wbi-password") as HTMLInputElement;
    expect(password.value).toBe("secret");
  });

  test("renders azure fields when storageType is 'azure'", () => {
    setFileUploadNode("f1", {
      storageType: "azure",
      azureStorageAccountName: "acct",
      azureStorageAccountKey: "key",
      azureBlobContainerName: "container",
      path: "uploads",
    });
    render(<FileUploadBlockForm blockId="f1" />);

    expect(screen.getByText("Storage Type")).toBeDefined();
    expect(screen.getByText("Storage Account Name")).toBeDefined();
    expect(screen.getByText("Storage Account Key")).toBeDefined();
    expect(screen.getByText("Blob Container Name")).toBeDefined();
    expect(screen.getByText("(Optional) Folder Path")).toBeDefined();

    // S3 fields are not visible
    expect(screen.queryByText("AWS Access Key ID")).toBeNull();
    expect(screen.queryByText("AWS Secret Access Key")).toBeNull();
    expect(screen.queryByText("S3 Bucket")).toBeNull();
    expect(screen.queryByText("Region Name")).toBeNull();
    expect(screen.queryByText("Google Account")).toBeNull();
    expect(screen.queryByText("Google Drive Folder ID")).toBeNull();

    // 3 textareas (account name, container, path) and 1 password input
    const textareas = screen.getAllByTestId("wbi-textarea");
    expect(textareas).toHaveLength(3);
    const password = screen.getByTestId("wbi-password") as HTMLInputElement;
    expect(password.value).toBe("key");
  });

  test("renders google drive fields when storageType is 'google_drive'", () => {
    setFileUploadNode("f1", {
      storageType: "google_drive",
      googleCredentialId: "goac_123",
      googleDriveFolderId: "folder_123",
    });
    render(<FileUploadBlockForm blockId="f1" />);

    expect(screen.getByText("Google Account")).toBeDefined();
    expect(screen.getByText("Google Drive Folder ID (Required)")).toBeDefined();
    expect(
      (screen.getByTestId("google-oauth-selector") as HTMLInputElement).value,
    ).toBe("goac_123");
    expect(
      (screen.getByTestId("wbi-textarea") as HTMLTextAreaElement).value,
    ).toBe("folder_123");
  });

  test("switching storageType s3 -> azure swaps the conditional fields", () => {
    setFileUploadNode("f1", { storageType: "s3" });
    const { rerender } = render(<FileUploadBlockForm blockId="f1" />);

    expect(screen.getByText("AWS Access Key ID")).toBeDefined();
    expect(screen.queryByText("Storage Account Name")).toBeNull();

    setFileUploadNode("f1", { storageType: "azure" });
    rerender(<FileUploadBlockForm blockId="f1" />);

    expect(screen.queryByText("AWS Access Key ID")).toBeNull();
    expect(screen.getByText("Storage Account Name")).toBeDefined();
  });

  test("editing storageType propagates", () => {
    setFileUploadNode("f1", { storageType: "s3" });
    render(<FileUploadBlockForm blockId="f1" />);

    fireEvent.change(screen.getByTestId("storage-type-select"), {
      target: { value: "google_drive" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("f1", {
      storageType: "google_drive",
    });
  });

  test("editing s3Bucket (when s3) propagates", () => {
    setFileUploadNode("f1", { storageType: "s3" });
    render(<FileUploadBlockForm blockId="f1" />);

    // Order: awsAccessKeyId, s3Bucket, regionName, path
    const textareas = screen.getAllByTestId("wbi-textarea");
    fireEvent.change(textareas[1] as HTMLTextAreaElement, {
      target: { value: "new-bucket" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("f1", {
      s3Bucket: "new-bucket",
    });
  });

  test("editing awsAccessKeyId (when s3) propagates", () => {
    setFileUploadNode("f1", { storageType: "s3" });
    render(<FileUploadBlockForm blockId="f1" />);

    const textareas = screen.getAllByTestId("wbi-textarea");
    fireEvent.change(textareas[0] as HTMLTextAreaElement, {
      target: { value: "AKIA-NEW" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("f1", {
      awsAccessKeyId: "AKIA-NEW",
    });
  });

  test("editing awsSecretAccessKey (when s3) propagates", () => {
    setFileUploadNode("f1", { storageType: "s3" });
    render(<FileUploadBlockForm blockId="f1" />);

    fireEvent.change(screen.getByTestId("wbi-password"), {
      target: { value: "new-secret" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("f1", {
      awsSecretAccessKey: "new-secret",
    });
  });

  test("editing path (when s3) propagates", () => {
    setFileUploadNode("f1", { storageType: "s3" });
    render(<FileUploadBlockForm blockId="f1" />);

    // Order: awsAccessKeyId, s3Bucket, regionName, path
    const textareas = screen.getAllByTestId("wbi-textarea");
    fireEvent.change(textareas[3] as HTMLTextAreaElement, {
      target: { value: "uploads/2026" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("f1", {
      path: "uploads/2026",
    });
  });

  test("editing azureStorageAccountName (when azure) propagates", () => {
    setFileUploadNode("f1", { storageType: "azure" });
    render(<FileUploadBlockForm blockId="f1" />);

    // Order: azureStorageAccountName, azureBlobContainerName, path
    const textareas = screen.getAllByTestId("wbi-textarea");
    fireEvent.change(textareas[0] as HTMLTextAreaElement, {
      target: { value: "newaccount" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("f1", {
      azureStorageAccountName: "newaccount",
    });
  });

  test("non-editable: edits do not propagate", () => {
    setFileUploadNode("f1", { storageType: "s3", editable: false });
    render(<FileUploadBlockForm blockId="f1" />);

    const textareas = screen.getAllByTestId("wbi-textarea");
    fireEvent.change(textareas[1] as HTMLTextAreaElement, {
      target: { value: "blocked" },
    });
    fireEvent.change(screen.getByTestId("wbi-password"), {
      target: { value: "blocked" },
    });

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("registers/unregisters commit", () => {
    setFileUploadNode("f1");
    const { unmount } = render(<FileUploadBlockForm blockId="f1" />);
    expect(usePendingCommitsStore.getState().commits["f1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["f1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setFileUploadNode("f1");
    render(<FileUploadBlockForm blockId="f1" />);

    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("f1");
    });
    expect(ok).toBe(true);
  });
});
