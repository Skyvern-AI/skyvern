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

import type { GoogleSheetsReadNode } from "../../nodes/GoogleSheetsReadNode/types";

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
    templateMode: boolean;
    onTemplateModeChange: (enabled: boolean) => void;
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
      data-template-mode={String(props.templateMode)}
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
      <button
        data-testid="spreadsheet-template-mode-on"
        onClick={() => props.onTemplateModeChange(true)}
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
    templateMode: boolean;
    onTemplateModeChange: (enabled: boolean) => void;
    onChange: (value: string) => void;
    onSelect: (tabName: string) => void;
  }) => (
    <div
      data-testid="sheet-tab-combobox"
      data-value={props.value}
      data-template-mode={String(props.templateMode)}
    >
      <input
        data-testid="sheet-tab-input"
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
      />
      <button
        data-testid="sheet-tab-select"
        onClick={() => props.onSelect("Tab2")}
      />
      <button
        data-testid="sheet-template-mode-on"
        onClick={() => props.onTemplateModeChange(true)}
      />
    </div>
  ),
}));

vi.mock("@/hooks/useGoogleOAuthCredentials", () => ({
  GOOGLE_SHEETS_REQUIRED_SCOPES: [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
  ],
  useGoogleOAuthCredentials: () => ({
    credentials: [{ id: "cred_42", valid: true }],
    isLoading: false,
    isFetching: false,
  }),
}));

vi.mock("@/hooks/useGoogleSpreadsheet", () => ({
  useGoogleSpreadsheet: () => ({ data: null }),
}));

vi.mock("@/hooks/useCurrentOrgId", () => ({
  useCurrentOrgId: () => "org_test",
}));

vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn() }),
}));

vi.mock("@/components/ui/switch", () => ({
  Switch: (props: {
    checked: boolean;
    onCheckedChange: (checked: boolean) => void;
    disabled?: boolean;
  }) => (
    <button
      role="switch"
      aria-checked={props.checked}
      data-testid={`switch-${props.checked}`}
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
import { GoogleSheetsReadBlockForm } from "./GoogleSheetsReadBlockForm";

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

function setGoogleSheetsReadNode(
  id: string,
  overrides: Partial<GoogleSheetsReadNode["data"]> = {},
) {
  mockNodes.set(id, {
    id,
    type: "googleSheetsRead",
    data: {
      debuggable: true,
      label: "google_sheets_read_1",
      continueOnFailure: false,
      editable: true,
      model: null,
      spreadsheetUrl: "",
      sheetName: "",
      range: "",
      credentialId: "",
      hasHeaderRow: true,
      parameterKeys: [],
      ...overrides,
    },
  });
}

describe("GoogleSheetsReadBlockForm (SKY-9361)", () => {
  test("returns null for missing node", () => {
    const { container } = render(
      <GoogleSheetsReadBlockForm blockId="missing" />,
    );
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("g1", { id: "g1", type: "task", data: {} });
    const { container } = render(<GoogleSheetsReadBlockForm blockId="g1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders Connection section with Google Account + Spreadsheet", () => {
    setGoogleSheetsReadNode("g1");
    render(<GoogleSheetsReadBlockForm blockId="g1" />);

    expect(screen.getByText("Connection")).toBeDefined();
    expect(screen.getByText("Google Account")).toBeDefined();
    expect(screen.getByText("Spreadsheet")).toBeDefined();
    expect(
      screen.getByTestId("google-oauth-credential-selector"),
    ).toBeDefined();
    expect(screen.getByTestId("spreadsheet-combobox")).toBeDefined();
  });

  test("renders Data section with Sheet Name + Range + Has Header Row", () => {
    setGoogleSheetsReadNode("g1");
    render(<GoogleSheetsReadBlockForm blockId="g1" />);

    expect(screen.getByText("Sheet Name")).toBeDefined();
    expect(screen.getByText("Range")).toBeDefined();
    expect(screen.getByText("Has Header Row")).toBeDefined();
    expect(screen.getByTestId("sheet-tab-combobox")).toBeDefined();
    expect(
      screen.getByTestId(
        "wbi-ph-A1:D10, MyNamedRange, or leave empty for all rows",
      ),
    ).toBeDefined();
  });

  test("selecting a Google account propagates credentialId", () => {
    setGoogleSheetsReadNode("g1");
    render(<GoogleSheetsReadBlockForm blockId="g1" />);

    fireEvent.click(screen.getByTestId("oauth-change"));

    expect(updateNodeData).toHaveBeenCalledWith("g1", {
      credentialId: "cred_42",
    });
  });

  test("changing spreadsheetUrl with same id keeps sheetName", () => {
    setGoogleSheetsReadNode("g1", {
      spreadsheetUrl: "https://docs.google.com/spreadsheets/d/abc123/edit",
      sheetName: "Sheet1",
    });
    render(<GoogleSheetsReadBlockForm blockId="g1" />);

    // Same id but partially mid-edit (no parseable URL yet)
    const input = screen.getByTestId("spreadsheet-input") as HTMLInputElement;
    fireEvent.change(input, {
      target: { value: "https://docs.google.com/spreadsheets/d/abc123/" },
    });

    expect(updateNodeData).toHaveBeenLastCalledWith("g1", {
      spreadsheetUrl: "https://docs.google.com/spreadsheets/d/abc123/",
    });
  });

  test("changing spreadsheetUrl to a new id clears sheetName", () => {
    setGoogleSheetsReadNode("g1", {
      spreadsheetUrl: "https://docs.google.com/spreadsheets/d/abc123/edit",
      sheetName: "Sheet1",
    });
    render(<GoogleSheetsReadBlockForm blockId="g1" />);

    const input = screen.getByTestId("spreadsheet-input") as HTMLInputElement;
    fireEvent.change(input, {
      target: { value: "https://docs.google.com/spreadsheets/d/xyz456/edit" },
    });

    expect(updateNodeData).toHaveBeenLastCalledWith("g1", {
      spreadsheetUrl: "https://docs.google.com/spreadsheets/d/xyz456/edit",
      sheetName: "",
    });
  });

  test("selecting a spreadsheet propagates url + first sheet name", () => {
    setGoogleSheetsReadNode("g1");
    render(<GoogleSheetsReadBlockForm blockId="g1" />);

    fireEvent.click(screen.getByTestId("spreadsheet-select"));

    expect(updateNodeData).toHaveBeenLastCalledWith("g1", {
      spreadsheetUrl: "https://docs.google.com/spreadsheets/d/abc123/edit",
      sheetName: "Sheet1",
    });
  });

  test("switching blocks clears the selected spreadsheet display name", () => {
    setGoogleSheetsReadNode("g1");
    setGoogleSheetsReadNode("g2", {
      spreadsheetUrl: "https://docs.google.com/spreadsheets/d/xyz456/edit",
    });
    const { rerender } = render(<GoogleSheetsReadBlockForm blockId="g1" />);

    fireEvent.click(screen.getByTestId("spreadsheet-select"));
    expect(screen.getByTestId("spreadsheet-combobox").dataset.displayName).toBe(
      "My Sheet",
    );

    rerender(<GoogleSheetsReadBlockForm blockId="g2" />);

    expect(screen.getByTestId("spreadsheet-combobox").dataset.displayName).toBe(
      "",
    );
    expect(screen.getByTestId("spreadsheet-combobox").dataset.value).toBe(
      "https://docs.google.com/spreadsheets/d/xyz456/edit",
    );
  });

  test("repeated true requests keep spreadsheet template mode on", () => {
    setGoogleSheetsReadNode("g1", {
      spreadsheetUrl: "{{ target_spreadsheet_url }}",
    });
    render(<GoogleSheetsReadBlockForm blockId="g1" />);

    const requestTemplateMode = screen.getByTestId(
      "spreadsheet-template-mode-on",
    );
    fireEvent.click(requestTemplateMode);
    fireEvent.click(requestTemplateMode);

    expect(
      screen
        .getByRole("button", { name: "Pick from your spreadsheets" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("repeated true requests keep sheet template mode on", () => {
    setGoogleSheetsReadNode("g1", {
      sheetName: "{{ target_sheet_name }}",
    });
    render(<GoogleSheetsReadBlockForm blockId="g1" />);

    const requestTemplateMode = screen.getByTestId("sheet-template-mode-on");
    fireEvent.click(requestTemplateMode);
    fireEvent.click(requestTemplateMode);

    expect(
      screen
        .getByRole("button", { name: "Pick from your sheet tabs" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("selecting a sheet propagates sheetName", () => {
    setGoogleSheetsReadNode("g1");
    render(<GoogleSheetsReadBlockForm blockId="g1" />);

    fireEvent.click(screen.getByTestId("sheet-tab-select"));

    expect(updateNodeData).toHaveBeenCalledWith("g1", { sheetName: "Tab2" });
  });

  test("editing range propagates", () => {
    setGoogleSheetsReadNode("g1");
    render(<GoogleSheetsReadBlockForm blockId="g1" />);

    const rangeInput = screen.getByTestId(
      "wbi-ph-A1:D10, MyNamedRange, or leave empty for all rows",
    );
    fireEvent.change(rangeInput, { target: { value: "A1:D10" } });

    expect(updateNodeData).toHaveBeenCalledWith("g1", { range: "A1:D10" });
  });

  test("toggling hasHeaderRow propagates", () => {
    setGoogleSheetsReadNode("g1", { hasHeaderRow: true });
    render(<GoogleSheetsReadBlockForm blockId="g1" />);

    fireEvent.click(screen.getByTestId("switch-true"));

    expect(updateNodeData).toHaveBeenCalledWith("g1", { hasHeaderRow: false });
  });

  test("registers/unregisters commit on mount/unmount", () => {
    setGoogleSheetsReadNode("g1");
    const { unmount } = render(<GoogleSheetsReadBlockForm blockId="g1" />);

    expect(usePendingCommitsStore.getState().commits["g1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["g1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setGoogleSheetsReadNode("g1");
    render(<GoogleSheetsReadBlockForm blockId="g1" />);

    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("g1");
    });
    expect(ok).toBe(true);
  });
});
