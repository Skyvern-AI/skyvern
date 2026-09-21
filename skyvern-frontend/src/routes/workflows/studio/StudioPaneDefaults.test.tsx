// @vitest-environment jsdom

import { useLayoutEffect, useMemo } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useMountEffect } from "@/hooks/useMountEffect";
import { useStudioFirstRunStore } from "@/store/StudioFirstRunStore";

import { shouldOpenCopilotPaneForHandoff } from "../discoverCopilotHandoff";
import { StudioPaneDefaultsProvider } from "./StudioPaneDefaults";
import { useStudioPaneDefaults } from "./StudioPaneDefaultsContext";
import { StudioWorkflowDeletedContext } from "./StudioShellContext";
import { useStudioPanes } from "./useStudioPanes";

const { toastMock } = vi.hoisted(() => ({
  toastMock: vi.fn(),
}));

vi.mock("@/components/ui/use-toast", () => ({
  toast: toastMock,
}));

function PanesProbe() {
  const {
    panes,
    resolveLivePanes,
    togglePane,
    openPane,
    setOpenPanes,
    setPanesOrder,
  } = useStudioPanes();
  const location = useLocation();
  return (
    <div>
      <output data-testid="panes">{panes.join(",")}</output>
      <output data-testid="live-panes">{resolveLivePanes().join(",")}</output>
      <output data-testid="address">
        {location.pathname}
        {location.search}
        {location.hash}
      </output>
      <button onClick={() => togglePane("overview")}>toggle-overview</button>
      <button onClick={() => togglePane("copilot")}>toggle-copilot</button>
      <button onClick={() => setPanesOrder(["editor", "copilot"])}>
        reorder-copilot
      </button>
      <button onClick={() => setOpenPanes(["editor"])}>show-editor-only</button>
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
  workflowDeletedAt = null,
}: {
  path?: string;
  hasBlocks?: boolean;
  stageWidth?: number;
  workflowDeletedAt?: string | null;
} = {}) {
  const tree = (
    <MemoryRouter initialEntries={[path]}>
      <StudioWorkflowDeletedContext.Provider value={workflowDeletedAt}>
        <StudioPaneDefaultsProvider hasBlocks={hasBlocks}>
          {stageWidth !== undefined ? <StageProbe width={stageWidth} /> : null}
          <PanesProbe />
        </StudioPaneDefaultsProvider>
      </StudioWorkflowDeletedContext.Provider>
    </MemoryRouter>
  );
  return { ...render(tree), tree };
}

function panesText(): string {
  return screen.getByTestId("panes").textContent ?? "";
}

function livePanesText(): string {
  return screen.getByTestId("live-panes").textContent ?? "";
}

function addressText(): string {
  return screen.getByTestId("address").textContent ?? "";
}

afterEach(cleanup);
beforeEach(() => {
  localStorage.clear();
  useStudioFirstRunStore.setState({
    coachMarkSeen: false,
    narrowNudgeSeen: false,
  });
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

  test("a run in the URL lands on Browser + Overview", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?wr=wr_1" });
    expect(panesText()).toBe("browser,overview");
  });

  test("a cold block-run deep link lands on Browser + Overview", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?wr=wr_1&bl=block_1" });
    expect(panesText()).toBe("browser,overview");
  });

  test("a blocks signal that changes after mount does not reshuffle the panes", () => {
    const { rerender } = renderStudio({ hasBlocks: false });
    expect(panesText()).toBe("copilot,browser");
    rerender(
      <MemoryRouter initialEntries={["/workflows/wpid_1/studio"]}>
        <StudioWorkflowDeletedContext.Provider value={null}>
          <StudioPaneDefaultsProvider hasBlocks={true}>
            <PanesProbe />
          </StudioPaneDefaultsProvider>
        </StudioWorkflowDeletedContext.Provider>
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

  test("toggling from the state default updates runtime panes without changing the URL", () => {
    renderStudio({ hasBlocks: false });
    fireEvent.click(screen.getByText("toggle-overview"));
    expect(panesText()).toBe("copilot,browser,overview");
    expect(addressText()).toBe("/workflows/wpid_1/studio");
  });
});

// Mirrors Workspace's mount-effect wiring for the handoff into the studio shell:
// open the Copilot pane once when a seeded prompt lands. `threadState` verifies
// that the pane hook retains an existing handoff when a caller omits state.
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
  test("a seeded handoff retains the default Copilot and Browser", () => {
    renderHandoff();
    expect(panesText()).toBe("copilot,browser");
  });

  test("no handoff prompt leaves the default panes untouched", () => {
    renderHandoff({ hasInitialCopilotMessage: false });
    expect(panesText()).toBe("copilot,browser");
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
        search: "?panes=browser",
        state: { copilotMessage: "Fill out the contact form" },
      },
    );
    expect(panesText()).toBe("browser,copilot");
    expect(messageText()).toBe("Fill out the contact form");
  });

  test("retains a seeded prompt when the pane caller omits route state", () => {
    renderHandoff(
      { threadState: false },
      {
        pathname: "/workflows/wpid_1/studio",
        search: "?panes=browser",
        state: { copilotMessage: "Fill out the contact form" },
      },
    );
    expect(panesText()).toBe("browser,copilot");
    expect(messageText()).toBe("Fill out the contact form");
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

  test("live panes include Copilot from the clamped shared selection", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?panes=copilot,editor,browser",
      stageWidth: 600,
    });
    expect(panesText()).toBe("copilot,editor");
    expect(livePanesText()).toBe("copilot,editor");
  });

  test("clamps a deleted workflow without restoring blocked Copilot", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?wr=wr_1",
      stageWidth: 500,
      workflowDeletedAt: "2026-08-12T00:00:00Z",
    });
    expect(panesText()).toBe("browser");
    expect(livePanesText()).toBe("browser");
  });

  test("a Copilot-only close does not reveal a clamp-hidden run pane", () => {
    const address = "/workflows/wpid_1/studio?panes=copilot,overview#proof";
    renderStudio({ path: address, stageWidth: 300 });
    expect(panesText()).toBe("copilot");

    fireEvent.click(screen.getByText("toggle-copilot"));
    expect(panesText()).toBe("");
    expect(addressText()).toBe(address);

    fireEvent.click(screen.getByText("toggle-copilot"));
    expect(panesText()).toBe("copilot");
    expect(addressText()).toBe(address);
  });

  test("a Copilot-only reorder does not reveal a clamp-hidden run pane", () => {
    const address =
      "/workflows/wpid_1/studio?panes=copilot,editor,overview#proof";
    renderStudio({ path: address, stageWidth: 600 });
    expect(panesText()).toBe("copilot,editor");

    fireEvent.click(screen.getByText("reorder-copilot"));

    expect(panesText()).toBe("editor,copilot");
    expect(addressText()).toBe(address);
  });

  test("an exact override removes clamp-hidden non-Copilot panes", () => {
    const address =
      "/workflows/wpid_1/studio?panes=copilot,editor,overview#proof";
    renderStudio({ path: address, stageWidth: 600 });
    expect(panesText()).toBe("copilot,editor");

    fireEvent.click(screen.getByText("show-editor-only"));

    expect(panesText()).toBe("editor");
    expect(addressText()).toBe(address);
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
    fireEvent.click(screen.getByText("toggle-overview"));
    expect(useStudioFirstRunStore.getState().coachMarkSeen).toBe(true);
  });
});
