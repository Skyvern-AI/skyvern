// @vitest-environment jsdom

import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  test,
  vi,
} from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { TooltipProvider } from "@/components/ui/tooltip";
import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import { type BlockSearchTarget } from "./blockSearch";
import { EditorPaneBlockSearch } from "./EditorPaneHeader";

// cmdk and Radix Popover need ResizeObserver and scrollIntoView, which jsdom
// lacks. Install them for this suite only and restore afterward.
class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const originalScrollIntoView = Element.prototype.scrollIntoView;

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  Element.prototype.scrollIntoView = () => {};
});

afterAll(() => {
  vi.unstubAllGlobals();
  if (originalScrollIntoView) {
    Element.prototype.scrollIntoView = originalScrollIntoView;
  } else {
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
  }
});

afterEach(cleanup);

const targets: Array<BlockSearchTarget> = [
  { nodeId: "login-node", label: "Login", blockType: "login" },
  { nodeId: "extract-node", label: "Extract rows", blockType: "extraction" },
  {
    nodeId: "email-node",
    label: "Send summary email",
    blockType: "send_email",
  },
];

const focusBlock = vi.fn();

function registerHandle(withTargets = targets) {
  useWorkflowBlockSearchStore.getState().registerHandle({
    getTargets: () => withTargets,
    focusBlock,
  });
}

function renderSearch() {
  return render(
    <TooltipProvider delayDuration={0}>
      <EditorPaneBlockSearch />
    </TooltipProvider>,
  );
}

function openSearch() {
  fireEvent.click(screen.getByRole("button", { name: "Search blocks" }));
  return screen.getByPlaceholderText("Search blocks…");
}

beforeEach(() => {
  focusBlock.mockReset();
  useWorkflowBlockSearchStore.getState().registerHandle(null);
  useWorkflowYamlEditorStore.getState().close();
});

describe("EditorPaneBlockSearch", () => {
  test("renders nothing while no canvas has registered a handle", () => {
    renderSearch();
    expect(screen.queryByRole("button", { name: "Search blocks" })).toBeNull();
  });

  test("hides while Code mode covers the canvas", () => {
    registerHandle();
    useWorkflowYamlEditorStore.getState().open("blocks: []");
    renderSearch();
    expect(screen.queryByRole("button", { name: "Search blocks" })).toBeNull();
  });

  test("lists every block on open and filters by case-insensitive substring", () => {
    registerHandle();
    renderSearch();
    const input = openSearch();

    expect(screen.getAllByRole("option")).toHaveLength(3);

    fireEvent.change(input, { target: { value: "ROWS" } });
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0]?.textContent).toContain("Extract rows");
  });

  test("shows an empty state when nothing matches", () => {
    registerHandle();
    renderSearch();
    const input = openSearch();

    fireEvent.change(input, { target: { value: "no such block" } });
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByText("No blocks found.")).toBeTruthy();
  });

  test("clicking a result jumps to that block and closes the popover", () => {
    registerHandle();
    renderSearch();
    const input = openSearch();

    fireEvent.change(input, { target: { value: "email" } });
    fireEvent.click(screen.getByRole("option"));

    expect(focusBlock).toHaveBeenCalledWith("email-node");
    expect(screen.queryByPlaceholderText("Search blocks…")).toBeNull();
  });

  test("Enter selects the highlighted (first) match", () => {
    registerHandle();
    renderSearch();
    const input = openSearch();

    fireEvent.change(input, { target: { value: "extract" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(focusBlock).toHaveBeenCalledWith("extract-node");
    expect(screen.queryByPlaceholderText("Search blocks…")).toBeNull();
  });

  test("Escape closes the popover without reaching the canvas's window handler", () => {
    const windowEscapeSpy = vi.fn();
    window.addEventListener("keydown", windowEscapeSpy);
    try {
      registerHandle();
      renderSearch();
      const input = openSearch();

      fireEvent.keyDown(input, { key: "Escape" });

      expect(screen.queryByPlaceholderText("Search blocks…")).toBeNull();
      expect(focusBlock).not.toHaveBeenCalled();
      // The stop keeps FlowRenderer's global Escape (clear selection) inert.
      expect(windowEscapeSpy).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", windowEscapeSpy);
    }
  });

  test("reopening after a search starts from a clean query", () => {
    registerHandle();
    renderSearch();
    const input = openSearch();

    fireEvent.change(input, { target: { value: "email" } });
    fireEvent.keyDown(input, { key: "Enter" });

    const reopened = openSearch();
    expect((reopened as HTMLInputElement).value).toBe("");
    expect(screen.getAllByRole("option")).toHaveLength(3);
  });
});
