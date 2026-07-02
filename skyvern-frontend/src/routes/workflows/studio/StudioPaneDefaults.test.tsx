// @vitest-environment jsdom

import { useLayoutEffect, useMemo } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useStudioFirstRunStore } from "@/store/StudioFirstRunStore";

import { StudioPaneDefaultsProvider } from "./StudioPaneDefaults";
import { useStudioPaneDefaults } from "./StudioPaneDefaultsContext";
import { useStudioPanes } from "./useStudioPanes";

const { runSignalsMock, toastMock } = vi.hoisted(() => ({
  runSignalsMock: vi.fn(),
  toastMock: vi.fn(),
}));

vi.mock("./useStudioRunSignals", () => ({
  useStudioRunSignals: () => runSignalsMock(),
}));

vi.mock("@/components/ui/use-toast", () => ({
  toast: toastMock,
}));

function PanesProbe() {
  const { panes, togglePane, openPane } = useStudioPanes();
  return (
    <div>
      <output data-testid="panes">{panes.join(",")}</output>
      <button onClick={() => togglePane("run")}>toggle-run</button>
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
  runSignalsMock.mockReturnValue({
    hasRun: false,
    runStatus: undefined,
    knownHasRuns: undefined,
  });
  toastMock.mockReset();
});

describe("state-aware default panes", () => {
  test("an agent with cached runs starts on Copilot + Browser", () => {
    runSignalsMock.mockReturnValue({
      hasRun: true,
      runStatus: undefined,
      knownHasRuns: true,
    });
    renderStudio();
    expect(panesText()).toBe("copilot,browser");
  });

  test("a never-run agent starts on Copilot + Editor", () => {
    runSignalsMock.mockReturnValue({
      hasRun: false,
      runStatus: undefined,
      knownHasRuns: false,
    });
    renderStudio();
    expect(panesText()).toBe("copilot,editor");
  });

  test("an empty agent starts on Copilot + Editor even before runs load", () => {
    renderStudio({ hasBlocks: false });
    expect(panesText()).toBe("copilot,editor");
  });

  test("an agent with blocks keeps the legacy default while runs are unknown", () => {
    renderStudio({ hasBlocks: true });
    expect(panesText()).toBe("copilot,browser");
  });

  test("a runs signal that arrives after mount does not reshuffle the panes", () => {
    const { rerender, tree } = renderStudio({ hasBlocks: true });
    expect(panesText()).toBe("copilot,browser");
    runSignalsMock.mockReturnValue({
      hasRun: false,
      runStatus: undefined,
      knownHasRuns: false,
    });
    rerender(tree);
    expect(panesText()).toBe("copilot,browser");
  });

  test("an explicit ?panes= is never overridden by the state default", () => {
    runSignalsMock.mockReturnValue({
      hasRun: false,
      runStatus: undefined,
      knownHasRuns: false,
    });
    renderStudio({ path: "/workflows/wpid_1/studio?panes=browser" });
    expect(panesText()).toBe("browser");
  });

  test("deep links keep their mapping regardless of the state default", () => {
    runSignalsMock.mockReturnValue({
      hasRun: true,
      runStatus: undefined,
      knownHasRuns: false,
    });
    renderStudio({ path: "/workflows/wpid_1/studio?wr=wr_1" });
    expect(panesText()).toBe("run");
  });

  test("toggling from the state default writes the default plus the change", () => {
    runSignalsMock.mockReturnValue({
      hasRun: true,
      runStatus: undefined,
      knownHasRuns: false,
    });
    renderStudio();
    fireEvent.click(screen.getByText("open-browser"));
    expect(panesText()).toBe("copilot,editor,browser");
  });
});

describe("narrow-viewport clamp of shared links", () => {
  const FOUR_PANES =
    "/workflows/wpid_1/studio?panes=copilot,editor,browser,run";

  test("an over-wide shared link degrades to its fitting prefix", () => {
    renderStudio({ path: FOUR_PANES, stageWidth: 600 });
    expect(panesText()).toBe("copilot,editor");
  });

  test("a wide viewport presents the shared link untouched", () => {
    renderStudio({ path: FOUR_PANES, stageWidth: 2000 });
    expect(panesText()).toBe("copilot,editor,browser,run");
  });

  test("without a measurable stage the list is presented as-is", () => {
    renderStudio({ path: FOUR_PANES });
    expect(panesText()).toBe("copilot,editor,browser,run");
  });

  test("the first pane write clears the clamp and builds on what is shown", () => {
    renderStudio({ path: FOUR_PANES, stageWidth: 600 });
    fireEvent.click(screen.getByText("toggle-run"));
    expect(panesText()).toBe("copilot,editor,run");
  });
});

describe("narrow-viewport nudge", () => {
  test("opening a pane past the min-width budget nudges exactly once", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?panes=copilot,editor",
      stageWidth: 600,
    });
    fireEvent.click(screen.getByText("toggle-run"));
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect(panesText()).toBe("copilot,editor,run");
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
      path: "/workflows/wpid_1/studio?panes=copilot,run",
      stageWidth: 600,
    });
    fireEvent.click(screen.getByText("toggle-run"));
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
