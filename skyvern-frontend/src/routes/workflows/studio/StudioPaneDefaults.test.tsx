// @vitest-environment jsdom

import { useLayoutEffect, useMemo } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useMountEffect } from "@/hooks/useMountEffect";
import { useStudioFirstRunStore } from "@/store/StudioFirstRunStore";
import { useStudioShellStore } from "@/store/StudioShellStore";

import { shouldOpenCopilotPaneForHandoff } from "../discoverCopilotHandoff";
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
  test("an empty agent starts on Editor + Browser", () => {
    renderStudio({ hasBlocks: false });
    expect(panesText()).toBe("editor,browser");
  });

  test("a built agent also starts on Editor + Browser", () => {
    renderStudio({ hasBlocks: true });
    expect(panesText()).toBe("editor,browser");
  });

  test("a run in the URL lands on Browser + Overview", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?wr=wr_1" });
    expect(panesText()).toBe("browser,overview");
  });

  test("a block-run deep link lands on Editor + Browser + Overview", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?wr=wr_1&bl=block_1" });
    expect(panesText()).toBe("editor,browser,overview");
  });

  test("a blocks signal that changes after mount does not reshuffle the panes", () => {
    const { rerender } = renderStudio({ hasBlocks: false });
    expect(panesText()).toBe("editor,browser");
    rerender(
      <MemoryRouter initialEntries={["/workflows/wpid_1/studio"]}>
        <StudioPaneDefaultsProvider hasBlocks={true}>
          <PanesProbe />
        </StudioPaneDefaultsProvider>
      </MemoryRouter>,
    );
    expect(panesText()).toBe("editor,browser");
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
    fireEvent.click(screen.getByText("toggle-overview"));
    expect(panesText()).toBe("editor,browser,overview");
  });
});

// Mirrors Workspace's mount-effect wiring for the handoff into the studio shell:
// open the Copilot pane once when a seeded prompt lands, threading the handoff
// route state through so the pane-open navigation does not wipe it. `threadState`
// lets a test reproduce the pre-fix bug where the state was dropped.
function HandoffProbe({
  embedded = true,
  hasInitialCopilotMessage = true,
  threadState = true,
}: {
  embedded?: boolean;
  hasInitialCopilotMessage?: boolean;
  threadState?: boolean;
}) {
  const location = useLocation();
  const { panes, openPane } = useStudioPanes();
  const copilotPaneOpen = panes.includes("copilot");
  const copilotMessage = (location.state as { copilotMessage?: string } | null)
    ?.copilotMessage;
  useMountEffect(() => {
    if (
      shouldOpenCopilotPaneForHandoff({
        embedded,
        hasInitialCopilotMessage,
        copilotPaneOpen,
      })
    ) {
      openPane("copilot", threadState ? { state: location.state } : undefined);
    }
  });
  return (
    <div>
      <output data-testid="panes">{panes.join(",")}</output>
      <output data-testid="copilot-message">{copilotMessage ?? ""}</output>
    </div>
  );
}

function renderHandoff(
  props: {
    embedded?: boolean;
    hasInitialCopilotMessage?: boolean;
    threadState?: boolean;
  } = {},
  entry: string | { pathname: string; search?: string; state?: unknown } = {
    pathname: "/workflows/wpid_1/studio",
    search: "?via=discover",
  },
) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <StudioPaneDefaultsProvider hasBlocks={false}>
        <HandoffProbe {...props} />
      </StudioPaneDefaultsProvider>
    </MemoryRouter>,
  );
}

function messageText(): string {
  return screen.getByTestId("copilot-message").textContent ?? "";
}

describe("Discover → Studio handoff opens the Copilot pane", () => {
  test("a seeded handoff opens Copilot on top of the default editor+browser", () => {
    renderHandoff();
    expect(panesText()).toBe("editor,browser,copilot");
  });

  test("no handoff prompt leaves the default panes untouched", () => {
    renderHandoff({ hasInitialCopilotMessage: false });
    expect(panesText()).toBe("editor,browser");
  });

  test("an explicit ?panes=copilot handoff is left as-is (no duplicate open)", () => {
    renderHandoff(
      {},
      {
        pathname: "/workflows/wpid_1/studio",
        search: "?via=discover&panes=copilot",
      },
    );
    expect(panesText()).toBe("copilot");
  });

  test("the pane-open navigation preserves the handoff route state (CTA has no sessionStorage fallback)", () => {
    renderHandoff(
      {},
      {
        pathname: "/workflows/wpid_1/studio",
        state: { copilotMessage: "Fill out the contact form" },
      },
    );
    expect(panesText()).toBe("editor,browser,copilot");
    expect(messageText()).toBe("Fill out the contact form");
  });

  test("a state-wiping open would drop the seeded prompt (regression guard)", () => {
    renderHandoff(
      { threadState: false },
      {
        pathname: "/workflows/wpid_1/studio",
        state: { copilotMessage: "Fill out the contact form" },
      },
    );
    expect(panesText()).toBe("editor,browser,copilot");
    expect(messageText()).toBe("");
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
    // default built-agent: editor,browser → toggle closes editor → browser
    expect(useStudioShellStore.getState().paneLayouts["edit"]).toEqual([
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
    // Pre-seed a layout distinct from the factory default so the restore is provable.
    useStudioShellStore.getState().setPaneLayout("edit", ["browser", "editor"]);

    renderStudio({ hasBlocks: true });
    expect(panesText()).toBe("browser,editor");
  });

  test("an empty agent ignores the learned layout and always shows factory default", () => {
    useStudioShellStore
      .getState()
      .setPaneLayout("edit", ["overview", "browser"]);

    renderStudio({ hasBlocks: false });
    expect(panesText()).toBe("editor,browser");
  });

  test("an explicit ?panes= beats the learned layout", () => {
    useStudioShellStore.getState().setPaneLayout("edit", ["editor", "browser"]);

    renderStudio({ path: "/workflows/wpid_1/studio?panes=overview" });
    expect(panesText()).toBe("overview");
  });

  test("a deep-link run URL beats the learned layout", () => {
    useStudioShellStore.getState().setPaneLayout("edit", ["editor", "browser"]);

    renderStudio({ path: "/workflows/wpid_1/studio?wr=wr_1" });
    expect(panesText()).toBe("browser,overview");
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
    expect(panesText()).toBe("editor,browser");
  });

  test("a corrupted non-array learned layout falls back without throwing", () => {
    useStudioShellStore.setState({
      paneLayouts: { edit: "corrupt" as never },
    });

    renderStudio({ hasBlocks: true });
    expect(panesText()).toBe("editor,browser");
  });
});
