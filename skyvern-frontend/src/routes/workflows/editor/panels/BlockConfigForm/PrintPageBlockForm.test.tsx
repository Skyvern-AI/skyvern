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
    // PrintPageEditor (nested under the form) subscribes via useNodesData;
    // mirror the same lookup so the fixture covers both reads.
    useNodesData: (id: string) => {
      const node = mockNodes.get(id);
      if (!node) return null;
      return { id: node.id, type: node.type, data: node.data };
    },
    useNodes: () => [],
    useEdges: () => [],
  };
});

vi.mock("../../workflowEditorUtils", () => ({
  getAvailableOutputParameterKeys: () => [],
}));

// Stub the shadcn Select to a native <select> so we can fire change events
// directly. The real Radix Select trigger relies on PointerEvent + portal
// rendering that isn't fully exercised in jsdom; the form only consumes
// `value` + `onValueChange`, so a native <select> covers the contract.
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
      data-testid="page-format-select"
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

vi.mock("../../nodes/TaskNode/ParametersMultiSelect", () => ({
  ParametersMultiSelect: ({
    parameters,
    onParametersChange,
  }: {
    parameters: Array<string>;
    onParametersChange: (next: Array<string>) => void;
    availableOutputParameters: Array<string>;
  }) => (
    <select
      data-testid="parameters-multi-select"
      multiple
      value={parameters}
      onChange={(e) =>
        onParametersChange(Array.from(e.target.selectedOptions, (o) => o.value))
      }
    >
      <option value="param_a">param_a</option>
      <option value="param_b">param_b</option>
    </select>
  ),
}));

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { PrintPageBlockForm } from "./PrintPageBlockForm";

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

function setPrintPageNode(
  id: string,
  data: Partial<{
    format: string;
    printBackground: boolean;
    includeTimestamp: boolean;
    customFilename: string;
    landscape: boolean;
    parameterKeys: Array<string>;
    editable: boolean;
  }> = {},
) {
  mockNodes.set(id, {
    id,
    type: "printPage",
    data: {
      format: data.format ?? "A4",
      printBackground: data.printBackground ?? true,
      includeTimestamp: data.includeTimestamp ?? true,
      customFilename: data.customFilename ?? "",
      landscape: data.landscape ?? false,
      parameterKeys: data.parameterKeys ?? [],
      editable: data.editable ?? true,
      label: "block_1",
      continueOnFailure: false,
      debuggable: true,
      model: null,
    },
  });
}

describe("PrintPageBlockForm (SKY-9361)", () => {
  test("returns null for missing node", () => {
    const { container } = render(<PrintPageBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("p1", { id: "p1", type: "task", data: {} });
    const { container } = render(<PrintPageBlockForm blockId="p1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders all 6 fields with current node data", () => {
    setPrintPageNode("p1", {
      format: "Letter",
      printBackground: false,
      includeTimestamp: true,
      customFilename: "report",
      landscape: true,
      parameterKeys: ["param_a"],
    });
    render(<PrintPageBlockForm blockId="p1" />);

    expect(screen.getByText("Page Format")).toBeDefined();
    expect(screen.getByText("Print Background")).toBeDefined();
    expect(screen.getByText("Headers & Footers")).toBeDefined();
    expect(screen.getByText("Custom Filename")).toBeDefined();
    expect(screen.getByText("Landscape")).toBeDefined();
    expect(screen.getByTestId("parameters-multi-select")).toBeDefined();

    const filenameInput = screen.getByPlaceholderText(
      "my_report",
    ) as HTMLInputElement;
    expect(filenameInput.value).toBe("report");

    // 3 switches: printBackground (false), includeTimestamp (true), landscape (true)
    const switches = screen.getAllByRole("switch");
    expect(switches).toHaveLength(3);
    expect(switches[0]?.getAttribute("aria-checked")).toBe("false"); // printBackground
    expect(switches[1]?.getAttribute("aria-checked")).toBe("true"); // includeTimestamp
    expect(switches[2]?.getAttribute("aria-checked")).toBe("true"); // landscape
  });

  test("switching format propagates via updateNodeData", () => {
    setPrintPageNode("p1");
    render(<PrintPageBlockForm blockId="p1" />);

    fireEvent.change(screen.getByTestId("page-format-select"), {
      target: { value: "Legal" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("p1", { format: "Legal" });
  });

  test("toggling printBackground propagates", () => {
    setPrintPageNode("p1", { printBackground: true });
    render(<PrintPageBlockForm blockId="p1" />);

    const switches = screen.getAllByRole("switch");
    fireEvent.click(switches[0] as HTMLElement); // printBackground

    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      printBackground: false,
    });
  });

  test("toggling includeTimestamp propagates", () => {
    setPrintPageNode("p1", { includeTimestamp: true });
    render(<PrintPageBlockForm blockId="p1" />);

    const switches = screen.getAllByRole("switch");
    fireEvent.click(switches[1] as HTMLElement); // includeTimestamp

    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      includeTimestamp: false,
    });
  });

  test("editing customFilename propagates", () => {
    setPrintPageNode("p1");
    render(<PrintPageBlockForm blockId="p1" />);

    fireEvent.change(screen.getByPlaceholderText("my_report"), {
      target: { value: "invoice_v2" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      customFilename: "invoice_v2",
    });
  });

  test("toggling landscape propagates", () => {
    setPrintPageNode("p1", { landscape: false });
    render(<PrintPageBlockForm blockId="p1" />);

    const switches = screen.getAllByRole("switch");
    fireEvent.click(switches[2] as HTMLElement); // landscape

    expect(updateNodeData).toHaveBeenCalledWith("p1", { landscape: true });
  });

  test("changing parameterKeys propagates via updateNodeData", () => {
    setPrintPageNode("p1");
    render(<PrintPageBlockForm blockId="p1" />);

    const select = screen.getByTestId(
      "parameters-multi-select",
    ) as HTMLSelectElement;
    const option = select.querySelector(
      'option[value="param_a"]',
    ) as HTMLOptionElement;
    option.selected = true;
    fireEvent.change(select);

    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      parameterKeys: ["param_a"],
    });
  });

  test("non-editable: edits do not propagate", () => {
    setPrintPageNode("p1", { editable: false });
    render(<PrintPageBlockForm blockId="p1" />);

    const switches = screen.getAllByRole("switch");
    fireEvent.click(switches[0] as HTMLElement);
    fireEvent.change(screen.getByPlaceholderText("my_report"), {
      target: { value: "blocked" },
    });

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("registers/unregisters commit", () => {
    setPrintPageNode("p1");
    const { unmount } = render(<PrintPageBlockForm blockId="p1" />);
    expect(usePendingCommitsStore.getState().commits["p1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["p1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setPrintPageNode("p1");
    render(<PrintPageBlockForm blockId="p1" />);

    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("p1");
    });
    expect(ok).toBe(true);
  });
});
