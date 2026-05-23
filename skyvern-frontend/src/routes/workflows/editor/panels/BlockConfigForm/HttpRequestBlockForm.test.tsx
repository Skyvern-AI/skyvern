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

import type { HttpRequestNode } from "../../nodes/HttpRequestNode/types";

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
    // HttpRequestEditor (nested under the form) subscribes via useNodesData
    // to stay reactive across sidebar saves; mirror getNode's stub here.
    useNodesData: (id: string) => {
      const node = mockNodes.get(id);
      if (!node) return null;
      return { id: node.id, type: node.type, data: node.data };
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

// Stub heavy components so we can drive the form's contract without spinning
// up full dialog/CodeMirror/Popover infrastructure in jsdom.
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

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: () => <span data-testid="help-tooltip" />,
}));

vi.mock("@/routes/workflows/components/CodeEditor", () => ({
  CodeEditor: (props: {
    value: string;
    onChange: (value: string) => void;
    language: string;
    minHeight?: string;
    maxHeight?: string;
  }) => (
    <textarea
      data-testid={`code-editor-${props.minHeight ?? "x"}-${props.maxHeight ?? "x"}`}
      data-language={props.language}
      data-min-height={props.minHeight}
      data-max-height={props.maxHeight}
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

vi.mock("../../nodes/WorkflowBlockParameterSelect", () => ({
  WorkflowBlockParameterSelect: (props: {
    nodeId: string;
    onAdd: (key: string) => void;
  }) => (
    <button
      data-testid="parameter-select-add"
      onClick={() => props.onAdd("user_id")}
    >
      add user_id
    </button>
  ),
}));

vi.mock("../../nodes/HttpRequestNode/CurlImportDialog", () => ({
  CurlImportDialog: ({ children }: { children: ReactNode }) => (
    <div data-testid="curl-import-dialog">{children}</div>
  ),
}));

vi.mock("../../nodes/HttpRequestNode/QuickHeadersDialog", () => ({
  QuickHeadersDialog: ({
    children,
    onAdd,
  }: {
    children: ReactNode;
    onAdd: (headers: Record<string, string>) => void;
  }) => (
    <div data-testid="quick-headers-dialog">
      <button
        data-testid="quick-headers-add"
        onClick={() => onAdd({ "X-Added": "yes" })}
      />
      {children}
    </div>
  ),
}));

vi.mock("../../nodes/HttpRequestNode/HttpUtils", () => ({
  MethodBadge: ({ method }: { method: string }) => (
    <span data-testid={`method-badge-${method}`}>{method}</span>
  ),
  UrlValidator: ({ url }: { url: string }) => (
    <span data-testid="url-validator" data-url={url} />
  ),
  RequestPreview: (props: {
    method: string;
    url: string;
    headers: string;
    body: string;
    files?: string;
  }) => (
    <div
      data-testid="request-preview"
      data-method={props.method}
      data-url={props.url}
    />
  ),
  JsonValidator: ({ value }: { value: string }) => (
    <span data-testid="json-validator" data-value={value} />
  ),
}));

// shadcn Switch is a Radix primitive that depends on PointerEvent semantics
// not fully simulated in jsdom. The form only consumes `checked` and
// `onCheckedChange`, so a button is enough to exercise the toggle path.
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

// Force the Accordion to always render so we can test advanced settings
// without needing to click the trigger. Mirrors the strategy used in
// FileDownloadBlockForm.test.tsx.
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

// Radix Popover positions content relative to the trigger; flatten so the
// "Add Parameter" content is always rendered and its onAdd is reachable.
vi.mock("@/components/ui/popover", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  return {
    Popover: Pass,
    PopoverTrigger: ({ children }: { children?: ReactNode }) => <>{children}</>,
    PopoverContent: Pass,
  };
});

// shadcn Select also relies on PointerEvent semantics. Replace with a plain
// HTML <select> that surfaces a deterministic test id.
vi.mock("@/components/ui/select", () => {
  const Select = ({
    value,
    onValueChange,
    disabled,
    children,
  }: {
    value: string;
    onValueChange?: (next: string) => void;
    disabled?: boolean;
    children?: ReactNode;
  }) => (
    <select
      data-testid="method-select"
      value={value}
      disabled={disabled}
      onChange={(event) => onValueChange?.(event.target.value)}
    >
      {children}
    </select>
  );
  const SelectTrigger = ({ children }: { children?: ReactNode }) => (
    <>{children}</>
  );
  const SelectContent = ({ children }: { children?: ReactNode }) => (
    <>{children}</>
  );
  const SelectItem = ({
    value,
    children,
  }: {
    value: string;
    children?: ReactNode;
  }) => <option value={value}>{children}</option>;
  return { Select, SelectTrigger, SelectContent, SelectItem };
});

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { HttpRequestBlockForm } from "./HttpRequestBlockForm";

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

function setHttpRequestNode(
  id: string,
  overrides: Partial<HttpRequestNode["data"]> = {},
) {
  mockNodes.set(id, {
    id,
    type: "http_request",
    data: {
      debuggable: true,
      label: "http_request_1",
      continueOnFailure: false,
      method: "POST",
      url: "",
      headers: "{}",
      body: "{}",
      files: "{}",
      timeout: 30,
      followRedirects: true,
      parameterKeys: [],
      editable: true,
      model: null,
      downloadFilename: "",
      saveResponseAsFile: false,
      ...overrides,
    },
  });
}

describe("HttpRequestBlockForm (SKY-9361)", () => {
  test("returns null for missing node", () => {
    const { container } = render(<HttpRequestBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("h1", { id: "h1", type: "task", data: {} });
    const { container } = render(<HttpRequestBlockForm blockId="h1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders Import cURL button at top", () => {
    setHttpRequestNode("h1");
    render(<HttpRequestBlockForm blockId="h1" />);

    expect(screen.getByTestId("curl-import-dialog")).toBeDefined();
    expect(screen.getByText("Import cURL")).toBeDefined();
  });

  test("renders Method/URL/Headers/Body/Files sections when method is POST", () => {
    setHttpRequestNode("h1", { method: "POST" });
    render(<HttpRequestBlockForm blockId="h1" />);

    expect(screen.getByText("Method")).toBeDefined();
    expect(screen.getByText("URL")).toBeDefined();
    expect(screen.getByText("Headers")).toBeDefined();
    expect(screen.getByText("Body")).toBeDefined();
    expect(screen.getByText("Files")).toBeDefined();
    expect(screen.getByTestId("request-preview")).toBeDefined();
  });

  test("hides Body and Files when method is GET", () => {
    setHttpRequestNode("h1", { method: "GET" });
    render(<HttpRequestBlockForm blockId="h1" />);

    expect(screen.getByText("Headers")).toBeDefined();
    expect(screen.queryByText("Body")).toBeNull();
    expect(screen.queryByText("Files")).toBeNull();
  });

  test("hides Body and Files when method is HEAD", () => {
    setHttpRequestNode("h1", { method: "HEAD" });
    render(<HttpRequestBlockForm blockId="h1" />);

    expect(screen.queryByText("Body")).toBeNull();
    expect(screen.queryByText("Files")).toBeNull();
  });

  test("hides Body and Files when method is DELETE", () => {
    setHttpRequestNode("h1", { method: "DELETE" });
    render(<HttpRequestBlockForm blockId="h1" />);

    expect(screen.queryByText("Body")).toBeNull();
    expect(screen.queryByText("Files")).toBeNull();
  });

  test("editing method propagates", () => {
    setHttpRequestNode("h1", { method: "GET" });
    render(<HttpRequestBlockForm blockId="h1" />);

    const select = screen.getByTestId("method-select") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "PUT" } });

    expect(updateNodeData).toHaveBeenCalledWith("h1", { method: "PUT" });
  });

  test("editing URL propagates", () => {
    setHttpRequestNode("h1");
    render(<HttpRequestBlockForm blockId="h1" />);

    const urlInput = screen.getByTestId(
      "wbi-ph-https://api.example.com/endpoint",
    );
    fireEvent.change(urlInput, {
      target: { value: "https://example.com/api" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("h1", {
      url: "https://example.com/api",
    });
  });

  test("editing headers (CodeEditor) propagates with empty-string fallback to '{}'", () => {
    setHttpRequestNode("h1");
    render(<HttpRequestBlockForm blockId="h1" />);

    // Headers and Files share min/max heights (80/160). Headers comes first
    // in document order; pick the first match.
    const headersEditor = screen.getAllByTestId("code-editor-80px-160px")[0]!;
    fireEvent.change(headersEditor, { target: { value: '{"X": "1"}' } });
    expect(updateNodeData).toHaveBeenCalledWith("h1", {
      headers: '{"X": "1"}',
    });

    updateNodeData.mockReset();
    fireEvent.change(headersEditor, { target: { value: "" } });
    expect(updateNodeData).toHaveBeenCalledWith("h1", { headers: "{}" });
  });

  test("editing body (CodeEditor) propagates with empty-string fallback to '{}'", () => {
    setHttpRequestNode("h1", { method: "POST" });
    render(<HttpRequestBlockForm blockId="h1" />);

    // body editor: minHeight=100px, maxHeight=200px
    const bodyEditor = screen.getByTestId("code-editor-100px-200px");
    fireEvent.change(bodyEditor, { target: { value: '{"a":1}' } });
    expect(updateNodeData).toHaveBeenCalledWith("h1", { body: '{"a":1}' });

    updateNodeData.mockReset();
    fireEvent.change(bodyEditor, { target: { value: "" } });
    expect(updateNodeData).toHaveBeenCalledWith("h1", { body: "{}" });
  });

  test("handleQuickHeaders merges new headers into existing JSON", () => {
    setHttpRequestNode("h1", {
      headers: JSON.stringify({ Existing: "value" }, null, 2),
    });
    render(<HttpRequestBlockForm blockId="h1" />);

    fireEvent.click(screen.getByTestId("quick-headers-add"));

    expect(updateNodeData).toHaveBeenCalledWith("h1", {
      headers: JSON.stringify({ Existing: "value", "X-Added": "yes" }, null, 2),
    });
  });

  test("handleQuickHeaders replaces when existing headers are invalid JSON", () => {
    setHttpRequestNode("h1", { headers: "not valid json" });
    render(<HttpRequestBlockForm blockId="h1" />);

    fireEvent.click(screen.getByTestId("quick-headers-add"));

    expect(updateNodeData).toHaveBeenCalledWith("h1", {
      headers: JSON.stringify({ "X-Added": "yes" }, null, 2),
    });
  });

  test("handleAddParameterToBody picks a unique param_N key", () => {
    setHttpRequestNode("h1", {
      method: "POST",
      body: JSON.stringify({ param_1: "x" }, null, 2),
    });
    render(<HttpRequestBlockForm blockId="h1" />);

    fireEvent.click(screen.getByTestId("parameter-select-add"));

    // existing has 1 key (param_1), so the next index starts at 2
    expect(updateNodeData).toHaveBeenCalledWith("h1", {
      body: JSON.stringify({ param_1: "x", param_2: "{{ user_id }}" }, null, 2),
    });
  });

  test("toggling saveResponseAsFile shows/hides Download Filename input", () => {
    setHttpRequestNode("h1", { saveResponseAsFile: false });
    const { rerender } = render(<HttpRequestBlockForm blockId="h1" />);

    expect(screen.queryByText("Download Filename")).toBeNull();

    setHttpRequestNode("h1", { saveResponseAsFile: true });
    rerender(<HttpRequestBlockForm blockId="h1" />);

    expect(screen.getByText("Download Filename")).toBeDefined();
  });

  test("editing timeout coerces to int with fallback to 30", () => {
    setHttpRequestNode("h1");
    render(<HttpRequestBlockForm blockId="h1" />);

    const timeoutInput = screen.getByDisplayValue("30") as HTMLInputElement;
    fireEvent.change(timeoutInput, { target: { value: "120" } });
    expect(updateNodeData).toHaveBeenCalledWith("h1", { timeout: 120 });

    updateNodeData.mockReset();
    fireEvent.change(timeoutInput, { target: { value: "abc" } });
    expect(updateNodeData).toHaveBeenCalledWith("h1", { timeout: 30 });
  });

  test("non-editable: edits do not propagate", () => {
    setHttpRequestNode("h1", { editable: false });
    render(<HttpRequestBlockForm blockId="h1" />);

    fireEvent.change(
      screen.getByTestId("wbi-ph-https://api.example.com/endpoint"),
      { target: { value: "https://example.com/" } },
    );
    // Headers editor (first occurrence of 80/160 pair).
    const headersEditor = screen.getAllByTestId("code-editor-80px-160px")[0]!;
    fireEvent.change(headersEditor, { target: { value: '{"X":"y"}' } });

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("registers/unregisters commit on mount/unmount", () => {
    setHttpRequestNode("h1");
    const { unmount } = render(<HttpRequestBlockForm blockId="h1" />);
    expect(usePendingCommitsStore.getState().commits["h1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["h1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setHttpRequestNode("h1");
    render(<HttpRequestBlockForm blockId="h1" />);

    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("h1");
    });
    expect(ok).toBe(true);
  });
});
