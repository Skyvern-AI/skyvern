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
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { TooltipProvider } from "@/components/ui/tooltip";
import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import { type BlockSearchTarget } from "./blockSearch";
import {
  EditorPaneBlockSearch,
  EditorPaneModeToggle,
} from "./EditorPaneHeader";

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
  useWorkflowYamlEditorStore.getState().registerEnterYamlMode(null);
  useWorkflowYamlEditorStore.getState().registerCommit(null);
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

describe("EditorPaneModeToggle", () => {
  function renderToggle() {
    return render(
      <TooltipProvider delayDuration={0}>
        <EditorPaneModeToggle />
      </TooltipProvider>,
    );
  }

  test("renders nothing until a canvas registers the YAML entry point", () => {
    renderToggle();
    expect(screen.queryByRole("button", { name: "Visual" })).toBeNull();
    expect(screen.queryByRole("button", { name: "YAML" })).toBeNull();
  });

  test("shows the Visual/YAML pair once registered, defaulting to Visual", () => {
    const store = useWorkflowYamlEditorStore.getState();
    store.registerEnterYamlMode(() => store.open("blocks: []"));
    renderToggle();

    const visual = screen.getByRole("button", { name: "Visual" });
    const yaml = screen.getByRole("button", { name: "YAML" });
    expect(visual.getAttribute("aria-pressed")).toBe("true");
    expect(yaml.getAttribute("aria-pressed")).toBe("false");
  });

  test("YAML enters yaml mode; Visual commits back out", async () => {
    const store = useWorkflowYamlEditorStore.getState();
    store.registerEnterYamlMode(() => store.open("blocks: []"));
    store.registerCommit(async () => {
      useWorkflowYamlEditorStore.getState().close();
      return true;
    });
    renderToggle();

    fireEvent.click(screen.getByRole("button", { name: "YAML" }));
    expect(useWorkflowYamlEditorStore.getState().active).toBe(true);
    expect(
      screen.getByRole("button", { name: "YAML", pressed: true }),
    ).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Visual" }));
    await waitFor(() =>
      expect(useWorkflowYamlEditorStore.getState().active).toBe(false),
    );
    expect(
      screen.getByRole("button", { name: "Visual", pressed: true }),
    ).not.toBeNull();
  });

  test("clicking the already-active segment does not re-fire its action", () => {
    const store = useWorkflowYamlEditorStore.getState();
    const enterYaml = vi.fn(() => store.open("blocks: []"));
    const commit = vi.fn(async () => {
      useWorkflowYamlEditorStore.getState().close();
      return true;
    });
    store.registerEnterYamlMode(enterYaml);
    store.registerCommit(commit);
    renderToggle();

    // Visual mode: clicking the active Visual segment must not commit.
    fireEvent.click(screen.getByRole("button", { name: "Visual" }));
    expect(commit).not.toHaveBeenCalled();

    // Clicking the now-active YAML segment must not re-enter — reserializing
    // the canvas would discard the in-progress YAML draft.
    fireEvent.click(screen.getByRole("button", { name: "YAML" }));
    expect(enterYaml).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "YAML" }));
    expect(enterYaml).toHaveBeenCalledTimes(1);
  });

  test("both toggles are disabled while a commit is in flight", () => {
    const store = useWorkflowYamlEditorStore.getState();
    store.registerEnterYamlMode(() => store.open("blocks: []"));
    store.setCommitting(true);
    renderToggle();

    expect(
      (screen.getByRole("button", { name: "Visual" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "YAML" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
});
