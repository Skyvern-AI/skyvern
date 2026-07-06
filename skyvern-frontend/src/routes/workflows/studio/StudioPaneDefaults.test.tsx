// @vitest-environment jsdom

import { useLayoutEffect, useMemo } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useStudioFirstRunStore } from "@/store/StudioFirstRunStore";
import { useStudioShellStore } from "@/store/StudioShellStore";

import { StudioPaneDefaultsProvider } from "./StudioPaneDefaults";
import { useStudioPaneDefaults } from "./StudioPaneDefaultsContext";
import { useStudioPanes } from "./useStudioPanes";

const { toastMock } = vi.hoisted(() => ({
  toastMock: vi.fn(),
}));

vi.mock("@/components/ui/use-toast", () => ({
  toast: toastMock,
}));

function PanesProbe() {
  const { panes, togglePane, openPane } = useStudioPanes();
  return (
    <div>
      <output data-testid="panes">{panes.join(",")}</output>
      <button onClick={() => togglePane("overview")}>toggle-overview</button>
      <button onClick={() => openPane("editor")}>open-editor</button>
      <button onClick={() => openPane("browser")}>open-browser</button>
    </div>
  );
}

// Simulates the shell's stage ref with an element of a known width.
function StageProbe({ width }: { width: number }) {
  const { registerStageElement } = useStudioPaneDefaults();
  const el = useMemo(() => {
    const div = document.createElement("div");
    Object.defineProperty(div, "clientWidth", { value: width });
    return div;
  }, [width]);
  useLayoutEffect(() => {
    registerStageElement(el);
  }, [registerStageElement, el]);
  return null;
}

function renderStudio({
  path = "/workflows/wpid_1/studio",
  hasBlocks = true,
  stageWidth,
}: {
  path?: string;
  hasBlocks?: boolean;
  stageWidth?: number;
} = {}) {
  const tree = (
    <MemoryRouter initialEntries={[path]}>
      <StudioPaneDefaultsProvider hasBlocks={hasBlocks}>
        {stageWidth !== undefined ? <StageProbe width={stageWidth} /> : null}
        <PanesProbe />
      </StudioPaneDefaultsProvider>
    </MemoryRouter>
  );
  return { ...render(tree), tree };
}

function panesText(): string {
  return screen.getByTestId("panes").textContent ?? "";
}

afterEach(cleanup);
beforeEach(() => {
  localStorage.clear();
  useStudioFirstRunStore.setState({
    coachMarkSeen: false,
    narrowNudgeSeen: false,
  });
  useStudioShellStore.getState().reset();
  toastMock.mockReset();
});

describe("cold-entry default panes (the four contexts)", () => {
  test("an empty agent starts on Copilot + Browser", () => {
    renderStudio({ hasBlocks: false });
    expect(panesText()).toBe("copilot,browser");
  });

  test("a built agent starts on Copilot + Browser + Editor", () => {
    renderStudio({ hasBlocks: true });
    expect(panesText()).toBe("copilot,browser,editor");
  });

  test("a run in the URL lands on Copilot + Browser + Overview", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?wr=wr_1" });
    expect(panesText()).toBe("copilot,browser,overview");
  });

  test("a block-run deep link lands on Editor + Browser + Overview", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?wr=wr_1&bl=block_1" });
    expect(panesText()).toBe("editor,browser,overview");
  });

  test("a blocks signal that changes after mount does not reshuffle the panes", () => {
    const { rerender } = renderStudio({ hasBlocks: false });
    expect(panesText()).toBe("copilot,browser");
    rerender(
      <MemoryRouter initialEntries={["/workflows/wpid_1/studio"]}>
        <StudioPaneDefaultsProvider hasBlocks={true}>
          <PanesProbe />
        </StudioPaneDefaultsProvider>
      </MemoryRouter>,
    );
    expect(panesText()).toBe("copilot,browser");
  });

  test("an explicit ?panes= is never overridden by the state default", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?panes=browser" });
    expect(panesText()).toBe("browser");
  });

  test("the pre-rename ?panes=run alias presents the Overview pane", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?panes=copilot,run" });
    expect(panesText()).toBe("copilot,overview");
  });

  test("toggling from the state default writes the default plus the change", () => {
    renderStudio({ hasBlocks: false });
    fireEvent.click(screen.getByText("open-editor"));
    expect(panesText()).toBe("copilot,browser,editor");
  });
});

describe("narrow-viewport clamp of shared links", () => {
  const FOUR_PANES =
    "/workflows/wpid_1/studio?panes=copilot,editor,browser,overview";

  test("an over-wide shared link degrades to its fitting prefix", () => {
    renderStudio({ path: FOUR_PANES, stageWidth: 600 });
    expect(panesText()).toBe("copilot,editor");
  });

  test("a wide viewport presents the shared link untouched", () => {
    renderStudio({ path: FOUR_PANES, stageWidth: 2000 });
    expect(panesText()).toBe("copilot,editor,browser,overview");
  });

  test("without a measurable stage the list is presented as-is", () => {
    renderStudio({ path: FOUR_PANES });
    expect(panesText()).toBe("copilot,editor,browser,overview");
  });

  test("the first pane write clears the clamp and builds on what is shown", () => {
    renderStudio({ path: FOUR_PANES, stageWidth: 600 });
    fireEvent.click(screen.getByText("toggle-overview"));
    expect(panesText()).toBe("copilot,editor,overview");
  });
});

describe("narrow-viewport nudge", () => {
  test("opening a pane past the min-width budget nudges exactly once", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?panes=copilot,editor",
      stageWidth: 600,
    });
    fireEvent.click(screen.getByText("toggle-overview"));
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect(panesText()).toBe("copilot,editor,overview");
    fireEvent.click(screen.getByText("open-browser"));
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect(useStudioFirstRunStore.getState().narrowNudgeSeen).toBe(true);
  });

  test("no nudge when the opened pane still fits", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?panes=copilot",
      stageWidth: 2000,
    });
    fireEvent.click(screen.getByText("open-browser"));
    expect(toastMock).not.toHaveBeenCalled();
  });

  test("closing a pane never nudges", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?panes=copilot,overview",
      stageWidth: 600,
    });
    fireEvent.click(screen.getByText("toggle-overview"));
    expect(panesText()).toBe("copilot");
    expect(toastMock).not.toHaveBeenCalled();
  });

  test("a pane write marks the coach mark as learned", () => {
    renderStudio({ stageWidth: 2000 });
    expect(useStudioFirstRunStore.getState().coachMarkSeen).toBe(false);
    fireEvent.click(screen.getByText("open-editor"));
    expect(useStudioFirstRunStore.getState().coachMarkSeen).toBe(true);
  });
});

// GestureLearningProbe calls togglePane with and without the learn flag so
// tests can verify which writes reach the store.
function GestureLearningProbe() {
  const { panes, togglePane } = useStudioPanes();
  return (
    <div>
      <output data-testid="panes">{panes.join(",")}</output>
      <button onClick={() => togglePane("editor", { learn: true })}>
        gesture-toggle-editor
      </button>
      <button onClick={() => togglePane("overview")}>
        system-toggle-overview
      </button>
    </div>
  );
}

function renderGestureStudio({
  path = "/workflows/wpid_1/studio",
  hasBlocks = true,
}: { path?: string; hasBlocks?: boolean } = {}) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <StudioPaneDefaultsProvider hasBlocks={hasBlocks}>
        <GestureLearningProbe />
      </StudioPaneDefaultsProvider>
    </MemoryRouter>,
  );
}

describe("gesture learning — edit-class writes reach the store", () => {
  test("a gesture toggle on an edit URL learns the resulting layout", () => {
    renderGestureStudio();
    fireEvent.click(screen.getByText("gesture-toggle-editor"));
    // default built-agent: copilot,browser,editor → toggle closes editor → copilot,browser
    expect(useStudioShellStore.getState().paneLayouts["edit"]).toEqual([
      "copilot",
      "browser",
    ]);
  });

  test("a system (non-gesture) toggle does NOT update the store", () => {
    renderGestureStudio({
      path: "/workflows/wpid_1/studio?panes=copilot,browser,overview",
    });
    fireEvent.click(screen.getByText("system-toggle-overview"));
    expect(useStudioShellStore.getState().paneLayouts["edit"]).toBeUndefined();
  });

  test("a gesture on a run URL learns the run class", () => {
    renderGestureStudio({
      path: "/workflows/wpid_1/studio?wr=wr_1&panes=copilot,browser,overview",
    });
    fireEvent.click(screen.getByText("gesture-toggle-editor"));
    expect(useStudioShellStore.getState().paneLayouts["run"]).toBeDefined();
    expect(useStudioShellStore.getState().paneLayouts["edit"]).toBeUndefined();
  });

  test("a gesture on a block-iterate URL (wr+bl) does not learn any class", () => {
    renderGestureStudio({
      path: "/workflows/wpid_1/studio?wr=wr_1&bl=block_1&panes=copilot,browser,overview",
    });
    fireEvent.click(screen.getByText("gesture-toggle-editor"));
    expect(useStudioShellStore.getState().paneLayouts).toEqual({});
  });
});

describe("restore from learned edit layout", () => {
  test("a built agent restores the last learned edit layout", () => {
    // Pre-seed the store as if the user previously arranged their panes.
    useStudioShellStore.getState().setPaneLayout("edit", ["editor", "browser"]);

    renderStudio({ hasBlocks: true });
    expect(panesText()).toBe("editor,browser");
  });

  test("an empty agent ignores the learned layout and always shows factory default", () => {
    useStudioShellStore.getState().setPaneLayout("edit", ["editor", "browser"]);

    renderStudio({ hasBlocks: false });
    expect(panesText()).toBe("copilot,browser");
  });

  test("an explicit ?panes= beats the learned layout", () => {
    useStudioShellStore.getState().setPaneLayout("edit", ["editor", "browser"]);

    renderStudio({ path: "/workflows/wpid_1/studio?panes=overview" });
    expect(panesText()).toBe("overview");
  });

  test("a deep-link run URL beats the learned layout", () => {
    useStudioShellStore.getState().setPaneLayout("edit", ["editor", "browser"]);

    renderStudio({ path: "/workflows/wpid_1/studio?wr=wr_1" });
    expect(panesText()).toBe("copilot,browser,overview");
  });

  test("a learned layout with an unknown pane id is sanitized to known ids only", () => {
    // Inject a stale persisted layout that contains a no-longer-valid id.
    useStudioShellStore.setState({
      paneLayouts: {
        edit: ["editor", "bogus" as never, "browser"],
      },
    });

    renderStudio({ hasBlocks: true });
    expect(panesText()).toBe("editor,browser");
  });

  test("a learned layout that becomes all-unknown falls back to the factory default", () => {
    useStudioShellStore.setState({
      paneLayouts: { edit: ["bogus" as never] },
    });

    renderStudio({ hasBlocks: true });
    expect(panesText()).toBe("copilot,browser,editor");
  });

  test("a corrupted non-array learned layout falls back without throwing", () => {
    useStudioShellStore.setState({
      paneLayouts: { edit: "corrupt" as never },
    });

    renderStudio({ hasBlocks: true });
    expect(panesText()).toBe("copilot,browser,editor");
  });
});
