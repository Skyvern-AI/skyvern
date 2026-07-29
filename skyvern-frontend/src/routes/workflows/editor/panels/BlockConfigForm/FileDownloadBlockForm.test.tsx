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

import { RunEngine } from "@/api/types";
import type { FileDownloadNode } from "../../nodes/FileDownloadNode/types";
import type {
  FileDownloadBlock,
  WorkflowApiResponse,
} from "../../../types/workflowTypes";

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
    useNodesData: (id: string) => {
      const node = mockNodes.get(id);
      return node ? { id: node.id, type: node.type, data: node.data } : null;
    },
    useNodes: () => Array.from(mockNodes.values()),
    useEdges: () => [],
  };
});

vi.mock("../../workflowEditorUtils", () => ({
  getAvailableOutputParameterKeys: () => [],
  isNodeInsideForLoop: () => false,
  getParentLoopSkipsOnFail: () => false,
}));

// Stub heavy components: WorkflowBlockInputTextarea, ParametersMultiSelect,
// ErrorCodeMappingEditor, ModelSelector, RunEngineSelector,
// BlockExecutionOptions, DisableCache. Each is replaced with a minimal stub
// that exposes the props the form passes so we can drive the form's
// contract without spinning up the full editor.
vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: (props: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
    aiImprove?: { useCase?: string };
  }) => {
    // The useCase string is `workflow_editor.<block>.<field>` so we can
    // route assertions for AI-improved fields by field. For non-AI fields
    // we fall back to placeholder as a stable disambiguator.
    const aiField = props.aiImprove?.useCase?.split(".").pop();
    const testId = aiField ? `wbi-${aiField}` : `wbi-ph-${props.placeholder}`;
    return (
      <textarea
        data-testid={testId}
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
      />
    );
  },
}));

vi.mock("@/components/ModelSelector", () => ({
  ModelSelector: (props: {
    value: unknown;
    onChange: (value: string) => void;
  }) => (
    <select
      data-testid="model-selector"
      value={props.value === null ? "" : String(props.value)}
      onChange={(event) => props.onChange(event.target.value)}
    >
      <option value="">none</option>
      <option value="model-a">model-a</option>
    </select>
  ),
}));

vi.mock("@/components/EngineSelector", () => ({
  RunEngineSelector: (props: {
    value: unknown;
    onChange: (value: string) => void;
  }) => (
    <select
      data-testid="engine-selector"
      value={props.value === null ? "" : String(props.value)}
      onChange={(event) => props.onChange(event.target.value)}
    >
      <option value="">none</option>
      <option value="skyvern-1.0">skyvern-1.0</option>
      <option value="skyvern-2.0">skyvern-2.0</option>
    </select>
  ),
}));

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: () => <span data-testid="help-tooltip" />,
}));

vi.mock("../../nodes/TaskNode/ParametersMultiSelect", () => ({
  ParametersMultiSelect: (props: {
    parameters: Array<string>;
    onParametersChange: (next: Array<string>) => void;
    availableOutputParameters: Array<string>;
  }) => (
    <div data-testid="parameters-multi-select">
      <button
        data-testid="parameters-change"
        onClick={() => props.onParametersChange(["param_a"])}
      />
    </div>
  ),
}));

vi.mock("../../nodes/components/BlockExecutionOptions", () => ({
  BlockExecutionOptions: (props: {
    continueOnFailure: boolean;
    nextLoopOnFailure?: boolean;
    blockType: string;
    isInsideForLoop: boolean;
    onContinueOnFailureChange: (checked: boolean) => void;
    onNextLoopOnFailureChange: (checked: boolean) => void;
  }) => (
    <div
      data-testid="block-execution-options"
      data-continue={String(props.continueOnFailure)}
      data-block-type={props.blockType}
      data-inside-loop={String(props.isInsideForLoop)}
    >
      <button
        data-testid="continue-on-failure-toggle"
        onClick={() =>
          props.onContinueOnFailureChange(!props.continueOnFailure)
        }
      />
    </div>
  ),
}));

vi.mock("../../nodes/DisableCache", () => ({
  DisableCache: (props: {
    disableCache: boolean;
    editable: boolean;
    onDisableCacheChange: (next: boolean) => void;
  }) => (
    <button
      data-testid="disable-cache-toggle"
      data-disabled={String(props.disableCache)}
      onClick={() => props.onDisableCacheChange(!props.disableCache)}
    />
  ),
}));

vi.mock("../../hooks/useSelectedCredentialTotpIdentifier", () => ({
  useSelectedCredentialTotpIdentifier: () => null,
}));

vi.mock("../../ErrorCodeMappingEditor", () => ({
  ErrorCodeMappingEditor: (props: {
    value: string;
    onChange: (value: string) => void;
    readOnly?: boolean;
  }) => (
    <textarea
      data-testid="error-code-mapping-editor"
      value={props.value}
      readOnly={props.readOnly}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

// shadcn Switch is a Radix primitive that depends on PointerEvent semantics
// not fully simulated in jsdom. The form only consumes `checked` +
// `onCheckedChange`, so a button is enough to exercise the toggle path.
vi.mock("@/components/ui/switch", () => ({
  Switch: (props: {
    "data-testid"?: string;
    checked: boolean;
    onCheckedChange: (checked: boolean) => void;
    disabled?: boolean;
  }) => (
    <button
      role="switch"
      aria-checked={props.checked}
      data-testid={props["data-testid"] ?? "error-code-mapping-switch"}
      disabled={props.disabled}
      onClick={() => props.onCheckedChange(!props.checked)}
    />
  ),
}));

// Force the Accordion content to always render so we can test advanced
// settings without needing to click the trigger. Mirrors the strategy from
// ValidationBlockForm.test.tsx where Radix collapses children when closed.
vi.mock("@/components/ui/accordion", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  return {
    Accordion: Pass,
    AccordionItem: Pass,
    AccordionTrigger: ({ children }: { children?: ReactNode }) => (
      <button data-testid="accordion-trigger">{children}</button>
    ),
    AccordionContent: Pass,
  };
});

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { errorMappingExampleValue } from "../../nodes/types";
import { FileDownloadBlockForm } from "./FileDownloadBlockForm";

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

function setFileDownloadNode(
  id: string,
  overrides: Partial<FileDownloadNode["data"]> = {},
) {
  mockNodes.set(id, {
    id,
    type: "fileDownload",
    data: {
      debuggable: true,
      label: "file_download_1",
      url: "",
      navigationGoal: "",
      errorCodeMapping: "null",
      maxRetries: null,
      maxStepsOverride: null,
      downloadSuffix: null,
      editable: true,
      parameterKeys: [],
      totpVerificationUrl: null,
      totpIdentifier: null,
      continueOnFailure: false,
      disableCache: false,
      engine: RunEngine.SkyvernV1,
      model: null,
      downloadTimeout: null,
      downloadTarget: "website",
      ...overrides,
    },
  });
}

describe("FileDownloadBlockForm (SKY-9361)", () => {
  test("returns null for missing node", () => {
    const { container } = render(<FileDownloadBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("d1", { id: "d1", type: "task", data: {} });
    const { container } = render(<FileDownloadBlockForm blockId="d1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders top-level url, navigationGoal, downloadTimeout", () => {
    setFileDownloadNode("d1", {
      url: "https://example.com/file.pdf",
      navigationGoal: "download the latest invoice",
      downloadTimeout: 90,
    });
    render(<FileDownloadBlockForm blockId="d1" />);

    expect(screen.getByText("URL")).toBeDefined();
    expect(screen.getByText("Download Goal")).toBeDefined();
    expect(screen.getByText("Download Timeout (sec)")).toBeDefined();
    expect(
      screen.getByText(
        "Once the file is downloaded, this block will complete.",
      ),
    ).toBeDefined();

    const navigationGoal = screen.getByTestId(
      "wbi-navigation_goal",
    ) as HTMLTextAreaElement;
    expect(navigationGoal.value).toBe("download the latest invoice");
  });

  test("renders Download Target defaulted to Website without destination fields", () => {
    setFileDownloadNode("d1");
    render(<FileDownloadBlockForm blockId="d1" />);

    expect(screen.getByText("Download Target")).toBeDefined();
    expect(screen.getByText("Website")).toBeDefined();
    expect(screen.queryByText("Prompt")).toBeNull();
    expect(screen.queryByText("S3 Bucket")).toBeNull();
    expect(screen.queryByText("Storage Account Name")).toBeNull();
    expect(screen.queryByText("Google Account")).toBeNull();
    expect(screen.queryByText("SFTP Host")).toBeNull();
  });

  test("editing url propagates", () => {
    setFileDownloadNode("d1");
    render(<FileDownloadBlockForm blockId="d1" />);

    const urlInput = screen.getByTestId("wbi-ph-https://");
    fireEvent.change(urlInput, {
      target: { value: "https://example.com/" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("d1", {
      url: "https://example.com/",
    });
  });

  test("editing navigationGoal propagates", () => {
    setFileDownloadNode("d1");
    render(<FileDownloadBlockForm blockId="d1" />);

    fireEvent.change(screen.getByTestId("wbi-navigation_goal"), {
      target: { value: "Download the file" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("d1", {
      navigationGoal: "Download the file",
    });
  });

  test("editing downloadTimeout writes the number", () => {
    setFileDownloadNode("d1");
    render(<FileDownloadBlockForm blockId="d1" />);

    const timeoutInput = screen.getByPlaceholderText("120") as HTMLInputElement;
    fireEvent.change(timeoutInput, { target: { value: "60" } });

    expect(updateNodeData).toHaveBeenCalledWith("d1", { downloadTimeout: 60 });
  });

  test("clearing downloadTimeout writes null", () => {
    setFileDownloadNode("d1", { downloadTimeout: 90 });
    render(<FileDownloadBlockForm blockId="d1" />);

    const timeoutInput = screen.getByPlaceholderText("120") as HTMLInputElement;
    fireEvent.change(timeoutInput, { target: { value: "" } });

    // The legacy falsy-check silently dropped the clear; new behavior
    // accepts empty input as "remove the override".
    expect(updateNodeData).toHaveBeenCalledWith("d1", {
      downloadTimeout: null,
    });
  });

  test("typing 0 writes 0 (no longer a silent-drop)", () => {
    setFileDownloadNode("d1");
    render(<FileDownloadBlockForm blockId="d1" />);

    const timeoutInput = screen.getByPlaceholderText("120") as HTMLInputElement;
    fireEvent.change(timeoutInput, { target: { value: "0" } });

    expect(updateNodeData).toHaveBeenCalledWith("d1", { downloadTimeout: 0 });
  });

  test("editing maxStepsOverride writes the number", () => {
    setFileDownloadNode("d1");
    render(<FileDownloadBlockForm blockId="d1" />);

    const maxStepsInput = screen.getByPlaceholderText(
      "Default: 10",
    ) as HTMLInputElement;
    fireEvent.change(maxStepsInput, { target: { value: "25" } });

    expect(updateNodeData).toHaveBeenCalledWith("d1", {
      maxStepsOverride: 25,
    });
  });

  test("clearing maxStepsOverride writes null", () => {
    setFileDownloadNode("d1", { maxStepsOverride: 25 });
    render(<FileDownloadBlockForm blockId="d1" />);

    const maxStepsInput = screen.getByPlaceholderText(
      "Default: 10",
    ) as HTMLInputElement;
    fireEvent.change(maxStepsInput, { target: { value: "" } });

    expect(updateNodeData).toHaveBeenCalledWith("d1", {
      maxStepsOverride: null,
    });
  });

  test("toggling errorCodeMapping switch on inserts the example JSON", () => {
    setFileDownloadNode("d1", { errorCodeMapping: "null" });
    render(<FileDownloadBlockForm blockId="d1" />);

    fireEvent.click(screen.getByTestId("error-code-mapping-switch"));

    expect(updateNodeData).toHaveBeenCalledWith("d1", {
      errorCodeMapping: JSON.stringify(errorMappingExampleValue, null, 2),
    });
  });

  test("toggling errorCodeMapping switch off restores 'null'", () => {
    setFileDownloadNode("d1", {
      errorCodeMapping: JSON.stringify(errorMappingExampleValue, null, 2),
    });
    render(<FileDownloadBlockForm blockId="d1" />);

    fireEvent.click(screen.getByTestId("error-code-mapping-switch"));

    expect(updateNodeData).toHaveBeenCalledWith("d1", {
      errorCodeMapping: "null",
    });
  });

  test("editing downloadSuffix propagates", () => {
    setFileDownloadNode("d1");
    render(<FileDownloadBlockForm blockId="d1" />);

    const suffixInput = screen.getByTestId(
      "wbi-ph-Enter the complete filename (without extension)",
    );
    fireEvent.change(suffixInput, { target: { value: "invoice-2024" } });

    expect(updateNodeData).toHaveBeenCalledWith("d1", {
      downloadSuffix: "invoice-2024",
    });
  });

  test("editing totpIdentifier propagates", () => {
    setFileDownloadNode("d1");
    render(<FileDownloadBlockForm blockId="d1" />);

    const totpInput = screen.getByTestId(
      "wbi-ph-Add an ID that links your TOTP to the block",
    );
    fireEvent.change(totpInput, { target: { value: "my-totp-id" } });

    expect(updateNodeData).toHaveBeenCalledWith("d1", {
      totpIdentifier: "my-totp-id",
    });
  });

  test("editing totpVerificationUrl propagates", () => {
    setFileDownloadNode("d1");
    render(<FileDownloadBlockForm blockId="d1" />);

    const totpUrlInput = screen.getByTestId("wbi-ph-Provide your 2FA endpoint");
    fireEvent.change(totpUrlInput, {
      target: { value: "https://totp.example.com/" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("d1", {
      totpVerificationUrl: "https://totp.example.com/",
    });
  });

  test("non-editable: edits do not propagate", () => {
    setFileDownloadNode("d1", { editable: false });
    render(<FileDownloadBlockForm blockId="d1" />);

    fireEvent.change(screen.getByTestId("wbi-navigation_goal"), {
      target: { value: "blocked" },
    });
    fireEvent.click(screen.getByTestId("disable-cache-toggle"));
    fireEvent.click(screen.getByTestId("error-code-mapping-switch"));

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("registers/unregisters commit", () => {
    setFileDownloadNode("d1");
    const { unmount } = render(<FileDownloadBlockForm blockId="d1" />);
    expect(usePendingCommitsStore.getState().commits["d1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["d1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setFileDownloadNode("d1");
    render(<FileDownloadBlockForm blockId="d1" />);

    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("d1");
    });
    expect(ok).toBe(true);
  });
});

describe("file download serialization", () => {
  test("omits destination fields for website downloads in saved and exported YAML", async () => {
    const { convert, getWorkflowBlocks } = await vi.importActual<
      typeof import("../../workflowEditorUtils")
    >("../../workflowEditorUtils");
    const destinationFields = [
      "download_target",
      "path",
      "prompt",
      "continue_on_empty",
      "s3_bucket",
      "aws_access_key_id",
      "aws_secret_access_key",
      "region_name",
      "azure_storage_account_name",
      "azure_storage_account_key",
      "azure_blob_container_name",
      "google_credential_id",
      "google_drive_folder_id",
      "sftp_host",
      "sftp_port",
      "sftp_username",
      "sftp_password",
      "sftp_private_key",
      "sftp_private_key_passphrase",
      "sftp_remote_path",
      "sftp_host_key",
    ];

    setFileDownloadNode("d1", {
      downloadTarget: "website",
      path: "{{ workflow_run_id }}",
      prompt: "stale prompt",
      s3Bucket: "stale-s3",
      azureBlobContainerName: "stale-azure",
      googleDriveFolderId: "stale-google",
      sftpHost: "stale-sftp",
      continueOnEmpty: true,
    });
    const node = mockNodes.get("d1") as FileDownloadNode;
    const [savedBlock] = getWorkflowBlocks([node], []);

    const apiBlock = {
      ...savedBlock,
      parameters: [],
      output_parameter: {},
      download_target: "website",
      path: "{{ workflow_run_id }}",
      prompt: "stale prompt",
      s3_bucket: "stale-s3",
      azure_blob_container_name: "stale-azure",
      google_drive_folder_id: "stale-google",
      sftp_host: "stale-sftp",
      continue_on_empty: true,
    } as unknown as FileDownloadBlock;
    const exportedBlock = convert({
      workflow_definition: {
        version: 2,
        parameters: [],
        blocks: [apiBlock],
      },
    } as unknown as WorkflowApiResponse).workflow_definition.blocks[0];

    for (const field of destinationFields) {
      expect(savedBlock).not.toHaveProperty(field);
      expect(exportedBlock).not.toHaveProperty(field);
    }
  });
});
