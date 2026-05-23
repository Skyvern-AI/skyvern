// @vitest-environment jsdom

import { act, cleanup, render, screen } from "@testing-library/react";
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
    type: "task",
    data: { label: id === "block-a" ? "Alpha" : "Beta" },
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
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { BLOCK_FORMS, type WorkflowBlockNodeType } from "./BlockConfigForm";
import { BlockConfigSidebar } from "./BlockConfigSidebar";

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
  useWorkflowPanelStore.getState().setSelectedBlockId(null);
  for (const key of BLOCK_FORM_KEYS) {
    BLOCK_FORMS[key] = StubFormForBlockType(key);
  }
});

afterEach(() => {
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
