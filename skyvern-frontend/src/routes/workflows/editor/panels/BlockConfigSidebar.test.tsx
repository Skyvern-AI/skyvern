// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import type { ComponentType } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// The BlockConfigSidebar imports the AppNode barrel and the icon module
// from the editor `nodes/` tree, which transitively pulls every block-type
// component. Stub the surface this test exercises: the type guard says
// "yes this is a block node" so the sidebar body looks up a block type;
// the icon renders a marker; and `workflowBlockTitle` resolves the
// fallback label. These mocks keep the test scoped to the structural
// invariant (mount-stable aside) rather than the body content.
vi.mock("../nodes", () => ({
  isWorkflowBlockNode: () => true,
}));
vi.mock("../nodes/WorkflowBlockIcon", () => ({
  WorkflowBlockIcon: () => <div data-testid="block-icon" />,
}));
vi.mock("../nodes/types", () => ({
  workflowBlockTitle: { task: "Task" },
}));

// The sidebar title delegates label edits to the same hook the canvas tile
// uses. Stub it so this test pins the wiring (title → handler) without
// pulling in React Flow node mutation, the parameters store, or the
// collapse store the real hook depends on.
const { mockLabelChangeHandler } = vi.hoisted(() => ({
  mockLabelChangeHandler: vi.fn(),
}));
vi.mock("@/routes/workflows/hooks/useLabelChangeHandler", () => ({
  useNodeLabelChangeHandler: ({
    initialValue,
  }: {
    id: string;
    initialValue: string;
  }) => [initialValue, mockLabelChangeHandler] as const,
}));

// `useReactFlow` requires a `<ReactFlowProvider>` ancestor with mounted
// nodes; spinning that up just to read `.getNode(id)` is overkill for a
// structural mount test. Stub it to a deterministic node lookup so
// switching `selectedBlockId` resolves to a different label without
// changing the surrounding tree.
vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  const nodeFor = (id: string) => ({
    id,
    type: id === "start-block" ? "start" : "task",
    data: {
      label:
        id === "block-a"
          ? "Alpha"
          : id === "start-block"
            ? "__start_block__"
            : id === "block-readonly"
              ? "ReadOnly"
              : "Beta",
      editable: id !== "block-readonly",
    },
  });
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => nodeFor(id),
    }),
    useNodesData: (id: string) => nodeFor(id),
  };
});

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import {
  BLOCK_SIDEBAR_WIDTH_MAX,
  useBlockSidebarWidthStore,
} from "@/store/BlockSidebarWidthStore";
import { useStudioShellStore } from "@/store/StudioShellStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { BLOCK_FORMS, type WorkflowBlockNodeType } from "./BlockConfigForm";
import { BlockConfigSidebar } from "./BlockConfigSidebar";
import { getContainedBlockSidebarWidth } from "../blockSidebar";

// EditableNodeTitle (used by the editable block title) measures truncation
// via ResizeObserver, which jsdom does not implement. Stubbed per-test in
// beforeEach/afterEach so the global does not leak across test files.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// The structural invariants this file pins are about the sidebar shell,
// not the body content. Stub each entry to a marker component so per-form
// tests handle routing.
const BLOCK_FORM_KEYS = Object.keys(BLOCK_FORMS) as WorkflowBlockNodeType[];
const ORIGINAL_BLOCK_FORMS = { ...BLOCK_FORMS };
const StubFormForBlockType: (
  blockType: WorkflowBlockNodeType,
) => ComponentType<{ blockId: string }> = (blockType) =>
  function Stub({ blockId }) {
    return (
      <div
        data-testid="block-config-form-stub"
        data-block-type={blockType}
        data-block-id={blockId}
      />
    );
  };

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  useWorkflowPanelStore.getState().setSelectedBlockId(null);
  useWorkflowPanelStore.getState().setWorkflowPanelState({
    active: false,
    content: "parameters",
  });
  useBlockSidebarWidthStore.getState().reset();
  useStudioShellStore.getState().reset();
  for (const key of BLOCK_FORM_KEYS) {
    BLOCK_FORMS[key] = StubFormForBlockType(key);
  }
});

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
  for (const key of BLOCK_FORM_KEYS) {
    BLOCK_FORMS[key] = ORIGINAL_BLOCK_FORMS[key];
  }
});

describe("BlockConfigSidebar mount stability (SKY-9360)", () => {
  test("renders the aside with the slide-in animation when a block is selected", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );

    const aside = screen.getByTestId("block-config-sidebar");
    expect(aside.tagName).toBe("ASIDE");
    // The slide-in animation classes are the load-bearing piece — assert
    // their presence rather than the full className string so layout
    // tweaks do not flake the test.
    expect(aside.className).toContain("animate-in");
    expect(aside.className).toContain("slide-in-from-right");
  });

  test("does NOT render anything when selectedBlockId is null", () => {
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("block-config-sidebar")).toBeNull();
  });

  test("preserves the same aside DOM node when selectedBlockId switches between blocks", () => {
    // Architectural invariant for SKY-9360: switching blocks must not
    // remount the sidebar shell, otherwise the slide-in animation re-fires
    // and the user perceives a flicker. SKY-9359 implemented this by
    // keeping `BlockConfigSidebarBody` *unkeyed* on `selectedBlockId` —
    // React reconciles in place and the aside DOM node persists across
    // the update. This test pins that contract via reference equality.
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );

    const asideBefore = screen.getByTestId("block-config-sidebar");
    expect(asideBefore).toBeDefined();

    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-b");
    });

    const asideAfter = screen.getByTestId("block-config-sidebar");
    // Reference equality — same DOM node, no remount, animation does not
    // re-fire. If a future change adds `key={selectedBlockId}` to the
    // aside (or to a wrapping ancestor), this assertion flips.
    expect(asideAfter).toBe(asideBefore);
  });

  test("unmounts the aside when selectedBlockId becomes null", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("block-config-sidebar")).not.toBeNull();

    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId(null);
    });
    expect(screen.queryByTestId("block-config-sidebar")).toBeNull();
  });

  test("renders the BlockConfigForm dispatcher in the body for the selected block (SKY-9361)", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );

    const stub = screen.getByTestId("block-config-form-stub");
    expect(stub.getAttribute("data-block-type")).toBe("task");
    expect(stub.getAttribute("data-block-id")).toBe("block-a");
  });
});

describe("BlockConfigSidebar footer label", () => {
  test("footer reads 'updated N sec ago' (not 'saved') so users do not confuse it with a Cmd+S persist", () => {
    useSidebarSaveStateStore.getState().setLastUpdatedAt("block-a", Date.now());
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );

    expect(screen.queryByText(/saved \d+ sec ago/i)).toBeNull();
    expect(screen.getByText(/updated \d+ sec ago/i)).toBeDefined();
  });
});

describe("BlockConfigSidebar mode gating (SKY-9361)", () => {
  test("does NOT render in build mode even when a block is selected", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    const { container } = render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/build"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );

    expect(container.firstChild).toBeNull();
  });

  test("renders in edit mode when a block is selected", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("block-config-sidebar")).toBeDefined();
  });
});

describe("BlockConfigSidebar block library layout (contained drawer)", () => {
  test("constrains the persisted sidebar width to the editor canvas gutter with numeric resize bounds", () => {
    const getBoundingClientRect = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        if (this.dataset.testid === "editor-shell") {
          return {
            width: 500,
            height: 800,
            top: 0,
            right: 500,
            bottom: 800,
            left: 0,
            x: 0,
            y: 0,
            toJSON: () => {},
          };
        }

        return {
          width: 0,
          height: 0,
          top: 0,
          right: 0,
          bottom: 0,
          left: 0,
          x: 0,
          y: 0,
          toJSON: () => {},
        };
      });

    try {
      act(() => {
        useBlockSidebarWidthStore.getState().setWidth(BLOCK_SIDEBAR_WIDTH_MAX);
        useWorkflowPanelStore.getState().setWorkflowPanelState({
          active: true,
          content: "nodeLibrary",
        });
      });

      render(
        <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
          <div data-testid="editor-shell">
            <BlockConfigSidebar />
          </div>
        </MemoryRouter>,
      );

      // aside → shell wrapper → Resizable root (the element carrying the width).
      const sidebarRoot = screen.getByTestId("block-config-sidebar")
        .parentElement?.parentElement;

      expect(getContainedBlockSidebarWidth(BLOCK_SIDEBAR_WIDTH_MAX, 500)).toBe(
        452,
      );
      expect(sidebarRoot?.style.width).toBe("452px");
      expect(sidebarRoot?.style.minWidth).toBe("320px");
      expect(sidebarRoot?.style.maxWidth).toBe("452px");
      expect(sidebarRoot?.style.cssText).not.toContain("min(");
      expect(useBlockSidebarWidthStore.getState().renderedWidth).toBe(452);
    } finally {
      getBoundingClientRect.mockRestore();
    }
  });

  test("lets block-library item labels shrink inside the bounded drawer", () => {
    act(() => {
      useWorkflowPanelStore.getState().setWorkflowPanelState({
        active: true,
        content: "nodeLibrary",
      });
    });

    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );

    const item = screen.getByTestId("block-library-item-login");
    const title = screen.getByText("Login Block");

    expect(item.className).toContain("min-w-0");
    expect(title.parentElement?.className).toContain("min-w-0");
    expect(title.className).toContain("min-w-0");
  });

  test("binds the search panel width to the drawer so it does not shrink to its content (SKY-11494)", () => {
    // The search input + results column live in a flex *row* parent, so without
    // w-full the panel sizes to its widest child — it shrinks as the query
    // narrows (e.g. "No results found") and leaves dead space when the drawer
    // widens. w-full ties it to the panel width instead.
    act(() => {
      useWorkflowPanelStore.getState().setWorkflowPanelState({
        active: true,
        content: "nodeLibrary",
      });
    });

    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );

    const panel = screen
      .getByPlaceholderText("Search blocks...")
      .closest(".flex-col");

    expect(panel).not.toBeNull();
    expect(panel?.className).toContain("w-full");
  });
});

describe("BlockConfigSidebar block title editing (SKY-10255)", () => {
  beforeEach(() => {
    mockLabelChangeHandler.mockClear();
  });

  test("clicking the block title reveals an input and committing a new value calls the label change handler", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );

    const title = screen.getByText("Alpha");
    fireEvent.click(title);

    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "renamed_block" } });
    fireEvent.blur(input);

    expect(mockLabelChangeHandler).toHaveBeenCalledWith("renamed_block");
  });

  test("keeps the start node title non-editable (no input on click)", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("start-block");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );

    const title = screen.getByText("Agent Settings");
    fireEvent.click(title);

    expect(screen.queryByRole("textbox")).toBeNull();
    expect(mockLabelChangeHandler).not.toHaveBeenCalled();
  });

  test("keeps the title non-editable for a read-only block (no input on click)", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-readonly");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );

    const title = screen.getByText("ReadOnly");
    fireEvent.click(title);

    expect(screen.queryByRole("textbox")).toBeNull();
    expect(mockLabelChangeHandler).not.toHaveBeenCalled();
  });
});

describe("BlockConfigSidebar settings collapse (SKY-11481)", () => {
  test("embedded studio renders the settings panel collapsed to a rail by default", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("start-block");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar embedded />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("settings-rail")).toBeDefined();
    expect(useStudioShellStore.getState().settingsCollapsed).toBe(true);
    // The body stays mounted (so the width animation can clip it) but inert.
    expect(screen.getByTestId("block-config-sidebar").className).toContain(
      "pointer-events-none",
    );
  });

  test("the rail is collapsible for any block, not just Agent Settings", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar embedded />
      </MemoryRouter>,
    );

    const rail = screen.getByTestId("settings-rail");
    expect(rail.textContent).toContain("Alpha");
    expect(screen.getByTestId("block-config-sidebar").className).toContain(
      "pointer-events-none",
    );
  });

  test("clicking the rail expands to the full settings panel", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("start-block");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar embedded />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Show settings" }));

    expect(screen.queryByTestId("settings-rail")).toBeNull();
    expect(screen.getByText("Agent Settings")).toBeDefined();
    // Expanded body is interactive again.
    expect(screen.getByTestId("block-config-sidebar").className).not.toContain(
      "pointer-events-none",
    );
  });

  test("the header chevron collapses the panel back to the rail", () => {
    act(() => {
      useStudioShellStore.getState().setSettingsCollapsed(false);
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar embedded />
      </MemoryRouter>,
    );

    expect(screen.queryByTestId("settings-rail")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Close block configuration" }),
    ).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Collapse settings" }));

    expect(screen.getByTestId("settings-rail")).toBeDefined();
    expect(useStudioShellStore.getState().settingsCollapsed).toBe(true);
    // Body stays mounted (clipped under the rail) but inert.
    expect(screen.getByTestId("block-config-sidebar").className).toContain(
      "pointer-events-none",
    );
  });

  test("the embedded shell uses the 0.75rem right inset mirroring the Copilot column", () => {
    // The 0.75rem inset now lives on the animated shell (Resizable root), not
    // the rail; collapsed and expanded both anchor there.
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("start-block");
    });
    const { rerender } = render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar embedded />
      </MemoryRouter>,
    );
    const railShell =
      screen.getByTestId("settings-rail").parentElement?.parentElement;
    expect(railShell?.style.right).toBe("0.75rem");

    act(() => {
      useStudioShellStore.getState().setSettingsCollapsed(false);
    });
    rerender(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar embedded />
      </MemoryRouter>,
    );
    const panelShell = screen.getByTestId("block-config-sidebar").parentElement
      ?.parentElement;
    expect(panelShell?.style.right).toBe("0.75rem");
  });

  test("legacy (non-embedded) editor does not collapse and keeps the close button", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
        <BlockConfigSidebar />
      </MemoryRouter>,
    );

    expect(screen.queryByTestId("settings-rail")).toBeNull();
    expect(screen.getByTestId("block-config-sidebar")).toBeDefined();
    expect(
      screen.getByRole("button", { name: "Close block configuration" }),
    ).toBeDefined();
  });
});
