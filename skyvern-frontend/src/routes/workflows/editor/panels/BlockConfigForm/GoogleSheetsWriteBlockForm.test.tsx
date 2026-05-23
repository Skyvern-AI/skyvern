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

import type { GoogleSheetsWriteNode } from "../../nodes/GoogleSheetsWriteNode/types";

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

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: () => <span data-testid="help-tooltip" />,
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: (props: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
  }) => (
    <textarea
      data-testid={`wbi-ph-${props.placeholder ?? ""}`}
      value={props.value}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
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

vi.mock("@/routes/workflows/components/GoogleOAuthCredentialSelector", () => ({
  GoogleOAuthCredentialSelector: (props: {
    nodeId: string;
    value: string;
    onChange: (value: string) => void;
  }) => (
    <div
      data-testid="google-oauth-credential-selector"
      data-value={props.value}
    >
      <button
        data-testid="oauth-change"
        onClick={() => props.onChange("cred_42")}
      />
    </div>
  ),
}));

vi.mock("@/routes/workflows/components/SpreadsheetCombobox", () => ({
  SpreadsheetCombobox: (props: {
    nodeId: string;
    credentialId: string;
    hasSelectedAccount: boolean;
    value: string;
    displayName: string | null;
    placeholder?: string;
    allowCreate: boolean;
    onChange: (value: string) => void;
    onSelect: (selection: {
      url: string;
      name: string;
      firstSheetName: string | null;
    }) => void;
  }) => (
    <div
      data-testid="spreadsheet-combobox"
      data-value={props.value}
      data-display-name={props.displayName ?? ""}
      data-has-selected-account={String(props.hasSelectedAccount)}
      data-allow-create={String(props.allowCreate)}
    >
      <input
        data-testid="spreadsheet-input"
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
      />
      <button
        data-testid="spreadsheet-select"
        onClick={() =>
          props.onSelect({
            url: "https://docs.google.com/spreadsheets/d/abc123/edit",
            name: "My Sheet",
            firstSheetName: "Sheet1",
          })
        }
      />
    </div>
  ),
}));

vi.mock("@/routes/workflows/components/SheetTabCombobox", () => ({
  SheetTabCombobox: (props: {
    nodeId: string;
    credentialId: string;
    hasSelectedAccount: boolean;
    spreadsheetUrl: string;
    value: string;
    placeholder?: string;
    allowCreate: boolean;
    onChange: (value: string) => void;
    onSelect: (tabName: string) => void;
  }) => (
    <div data-testid="sheet-tab-combobox" data-value={props.value}>
      <input
        data-testid="sheet-tab-input"
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
      />
      <button
        data-testid="sheet-tab-select"
        onClick={() => props.onSelect("Tab2")}
      />
    </div>
  ),
}));

vi.mock("@/routes/workflows/components/ColumnMappingEditor", () => ({
  ColumnMappingEditor: (props: {
    idScope: string;
    value: string;
    onChange: (next: string) => void;
    headers?: Array<{ letter: string; name: string }>;
    headersLoading?: boolean;
  }) => (
    <div data-testid="column-mapping-editor" data-value={props.value}>
      <button
        data-testid="column-mapping-change"
        onClick={() => props.onChange('{"name":"A"}')}
      />
    </div>
  ),
}));

vi.mock("@/hooks/useGoogleOAuthCredentials", () => ({
  useGoogleOAuthCredentials: () => ({
    credentials: [{ id: "cred_42", valid: true }],
    isLoading: false,
    isFetching: false,
  }),
}));

vi.mock("@/hooks/useGoogleSpreadsheet", () => ({
  useGoogleSpreadsheet: () => ({ data: null }),
}));

vi.mock("@/hooks/useGoogleSheetHeaders", () => ({
  useGoogleSheetHeaders: () => ({ data: [], isLoading: false, error: null }),
}));

vi.mock("@/hooks/useGoogleSheetDimensions", () => ({
  useGoogleSheetDimensions: () => ({ data: null, isLoading: false }),
}));

vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: (props: {
    checked: boolean;
    disabled?: boolean;
    onCheckedChange: (checked: boolean | "indeterminate") => void;
  }) => (
    <button
      role="checkbox"
      aria-checked={props.checked}
      data-testid={`checkbox-${props.checked}`}
      disabled={props.disabled}
      onClick={() => props.onCheckedChange(!props.checked)}
    />
  ),
}));

// Force the Accordion to always render so we can test all sections
// without needing to click triggers.
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
import { GoogleSheetsWriteBlockForm } from "./GoogleSheetsWriteBlockForm";

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

function setGoogleSheetsWriteNode(
  id: string,
  overrides: Partial<GoogleSheetsWriteNode["data"]> = {},
) {
  mockNodes.set(id, {
    id,
    type: "googleSheetsWrite",
    data: {
      debuggable: true,
      label: "google_sheets_write_1",
      continueOnFailure: false,
      editable: true,
      model: null,
      spreadsheetUrl: "",
      sheetName: "",
      range: "",
      credentialId: "",
      writeMode: "append",
      values: "",
      columnMapping: "",
      createSheetIfMissing: false,
      parameterKeys: [],
      ...overrides,
    },
  });
}

describe("GoogleSheetsWriteBlockForm (SKY-9361)", () => {
  test("returns null for missing node", () => {
    const { container } = render(
      <GoogleSheetsWriteBlockForm blockId="missing" />,
    );
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("g1", { id: "g1", type: "task", data: {} });
    const { container } = render(<GoogleSheetsWriteBlockForm blockId="g1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders Connection section + Data accordion + Advanced accordion", () => {
    setGoogleSheetsWriteNode("g1");
    render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    expect(screen.getByText("Connection")).toBeDefined();
    expect(screen.getByText("Google Account")).toBeDefined();
    expect(screen.getByText("Spreadsheet")).toBeDefined();
    expect(
      screen.getByTestId("google-oauth-credential-selector"),
    ).toBeDefined();
    expect(screen.getByTestId("spreadsheet-combobox")).toBeDefined();
    expect(screen.getByText("Sheet Name")).toBeDefined();
    expect(screen.getByText("Write Mode")).toBeDefined();
    expect(screen.getByText("Values")).toBeDefined();
    expect(screen.getByText(/Column Mapping/)).toBeDefined();
    expect(screen.getByText("Create sheet if missing")).toBeDefined();
  });

  test("selecting a Google account propagates credentialId", () => {
    setGoogleSheetsWriteNode("g1");
    render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    fireEvent.click(screen.getByTestId("oauth-change"));

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      credentialId: "cred_42",
    });
  });

  test("changing spreadsheetUrl with same id keeps sheetName", () => {
    setGoogleSheetsWriteNode("g1", {
      spreadsheetUrl: "https://docs.google.com/spreadsheets/d/abc123/edit",
      sheetName: "Sheet1",
    });
    render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    const input = screen.getByTestId("spreadsheet-input") as HTMLInputElement;
    fireEvent.change(input, {
      target: { value: "https://docs.google.com/spreadsheets/d/abc123/" },
    });

    expect(updateNodeData).toHaveBeenLastCalledWith("g1", {
      spreadsheetUrl: "https://docs.google.com/spreadsheets/d/abc123/",
    });
  });

  test("changing spreadsheetUrl to a new id clears sheetName", () => {
    setGoogleSheetsWriteNode("g1", {
      spreadsheetUrl: "https://docs.google.com/spreadsheets/d/abc123/edit",
      sheetName: "Sheet1",
    });
    render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    const input = screen.getByTestId("spreadsheet-input") as HTMLInputElement;
    fireEvent.change(input, {
      target: { value: "https://docs.google.com/spreadsheets/d/xyz456/edit" },
    });

    expect(updateNodeData).toHaveBeenLastCalledWith("g1", {
      spreadsheetUrl: "https://docs.google.com/spreadsheets/d/xyz456/edit",
      sheetName: "",
    });
  });

  test("selecting a sheet propagates sheetName", () => {
    setGoogleSheetsWriteNode("g1");
    render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    fireEvent.click(screen.getByTestId("sheet-tab-select"));

    expect(updateNodeData).toHaveBeenCalledWith("g1", { sheetName: "Tab2" });
  });

  test("editing range propagates (write mode update)", () => {
    setGoogleSheetsWriteNode("g1", { writeMode: "update" });
    render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    const rangeInput = screen.getByTestId("wbi-ph-A2:D5 or MyNamedRange");
    fireEvent.change(rangeInput, { target: { value: "A2:D5" } });

    expect(updateNodeData).toHaveBeenCalledWith("g1", { range: "A2:D5" });
  });

  test("switching writeMode to update propagates writeMode", () => {
    setGoogleSheetsWriteNode("g1", { writeMode: "append" });
    render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    fireEvent.click(screen.getByText("Update range"));

    expect(updateNodeData).toHaveBeenCalledWith("g1", { writeMode: "update" });
  });

  test("switching writeMode to append clears range", () => {
    setGoogleSheetsWriteNode("g1", { writeMode: "update", range: "A1:B2" });
    render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    fireEvent.click(screen.getByText("Append rows"));

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      writeMode: "append",
      range: "",
    });
  });

  test("editing values propagates", () => {
    setGoogleSheetsWriteNode("g1");
    const { container } = render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    // Append mode hides the Range textarea, so the only visible
    // WorkflowBlockInputTextarea is Values.
    const textareas = container.querySelectorAll("textarea");
    expect(textareas).toHaveLength(1);
    fireEvent.change(textareas[0]!, { target: { value: "[[1,2,3]]" } });

    expect(updateNodeData).toHaveBeenCalledWith("g1", { values: "[[1,2,3]]" });
  });

  test("editing columnMapping propagates", () => {
    setGoogleSheetsWriteNode("g1");
    render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    fireEvent.click(screen.getByTestId("column-mapping-change"));

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      columnMapping: '{"name":"A"}',
    });
  });

  test("toggling createSheetIfMissing propagates", () => {
    setGoogleSheetsWriteNode("g1", { createSheetIfMissing: false });
    render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    fireEvent.click(screen.getByTestId("checkbox-false"));

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      createSheetIfMissing: true,
    });
  });

  test("non-editable: propagates updates not blocked at this layer", () => {
    // useUpdate gates by editable; we exercise that the form still hands off
    // calls to update — gating happens inside useUpdate, not the form.
    setGoogleSheetsWriteNode("g1", { editable: false });
    render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    fireEvent.click(screen.getByTestId("oauth-change"));

    // useUpdate swallows when not editable; updateNodeData should not be called.
    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("registers/unregisters commit on mount/unmount", () => {
    setGoogleSheetsWriteNode("g1");
    const { unmount } = render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    expect(usePendingCommitsStore.getState().commits["g1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["g1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setGoogleSheetsWriteNode("g1");
    render(<GoogleSheetsWriteBlockForm blockId="g1" />);

    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("g1");
    });
    expect(ok).toBe(true);
  });
});
