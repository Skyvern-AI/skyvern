// @vitest-environment jsdom

import { useEffect, useLayoutEffect, useMemo, type ReactNode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useMountEffect } from "@/hooks/useMountEffect";
import { useStudioFirstRunStore } from "@/store/StudioFirstRunStore";
import { useManualSignInStore } from "@/store/useManualSignInStore";

import { shouldOpenEmberPaneForHandoff } from "../discoverEmberHandoff";
import { useWorkspaceEmberUpdate } from "../editor/Workspace";
import type { WorkflowVersion } from "../hooks/useWorkflowVersionsQuery";
import { StudioPaneDefaultsProvider } from "./StudioPaneDefaults";
import { useStudioPaneDefaults } from "./StudioPaneDefaultsContext";
import { useSwitchStudioRun } from "./runSwitchNavigation";
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
    closePane,
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
      <button onClick={() => togglePane("ember")}>toggle-copilot</button>
      <button onClick={() => togglePane("editor")}>toggle-editor</button>
      <button onClick={() => closePane("editor")}>close-editor</button>
      <button onClick={() => setPanesOrder(["editor", "ember"])}>
        reorder-copilot
      </button>
      <button onClick={() => setOpenPanes(["editor"])}>show-editor-only</button>
      <button onClick={() => setOpenPanes(["browser"], { byUser: true })}>
        user-shows-browser-only
      </button>
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
  children,
}: {
  path?: string;
  hasBlocks?: boolean;
  stageWidth?: number;
  workflowDeletedAt?: string | null;
  children?: ReactNode;
} = {}) {
  const tree = (
    <MemoryRouter initialEntries={[path]}>
      <StudioWorkflowDeletedContext.Provider value={workflowDeletedAt}>
        <StudioPaneDefaultsProvider hasBlocks={hasBlocks}>
          {stageWidth !== undefined ? <StageProbe width={stageWidth} /> : null}
          {children}
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

afterEach(() => {
  cleanup();
  useManualSignInStore.getState().setBrowserSessionId(null);
});
beforeEach(() => {
  localStorage.clear();
  useStudioFirstRunStore.setState({ narrowNudgeSeen: false });
  toastMock.mockReset();
});

describe("cold-entry default panes (the four contexts)", () => {
  test.each(["?via=blank", "?via=discover", "?record=1"])(
    "a new agent created with %s starts on Ember + Browser",
    (search) => {
      renderStudio({
        path: `/workflows/wpid_1/studio${search}`,
        hasBlocks: false,
      });
      expect(panesText()).toBe("ember,browser");
    },
  );

  test("a new agent created from a template keeps Ember + Browser + Editor", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?via=template" });
    expect(panesText()).toBe("ember,browser,editor");
  });

  test("drops a create-only ?via= once the panes resolve, so a reload opens as an Edit", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?via=blank&cache-key-value=x",
      hasBlocks: false,
    });
    expect(panesText()).toBe("ember,browser");
    expect(addressText()).toBe("/workflows/wpid_1/studio?cache-key-value=x");
  });

  test("keeps ?via=discover for the Ember handoff that strips it later", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?via=discover",
      hasBlocks: false,
    });
    expect(addressText()).toBe("/workflows/wpid_1/studio?via=discover");
  });

  test("Edit on an existing empty agent opens Ember + Editor", () => {
    renderStudio({ hasBlocks: false });
    expect(panesText()).toBe("ember,editor");
  });

  test("a built agent starts on Ember + Editor", () => {
    renderStudio({ hasBlocks: true });
    expect(panesText()).toBe("ember,editor");
  });

  test("a run in the URL lands on Overview + Browser", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?wr=wr_1" });
    expect(panesText()).toBe("overview,browser");
  });

  test("a cold block-run deep link lands on Overview + Browser", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?wr=wr_1&bl=block_1" });
    expect(panesText()).toBe("overview,browser");
  });

  test("a blocks signal that changes after mount does not reshuffle the panes", () => {
    const { rerender } = renderStudio({
      path: "/workflows/wpid_1/studio?via=blank",
      hasBlocks: false,
    });
    expect(panesText()).toBe("ember,browser");
    rerender(
      <MemoryRouter initialEntries={["/workflows/wpid_1/studio?via=blank"]}>
        <StudioWorkflowDeletedContext.Provider value={null}>
          <StudioPaneDefaultsProvider hasBlocks={true}>
            <PanesProbe />
          </StudioPaneDefaultsProvider>
        </StudioWorkflowDeletedContext.Provider>
      </MemoryRouter>,
    );
    expect(panesText()).toBe("ember,browser");
  });

  test("an explicit ?panes= is never overridden by the state default", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?panes=browser" });
    expect(panesText()).toBe("browser");
  });

  test("the pre-rename copilot and run pane ids present the Ember and Overview panes", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?panes=copilot,run" });
    expect(panesText()).toBe("ember,overview");
  });

  test("toggling from the state default updates runtime panes without changing the URL", () => {
    renderStudio({ hasBlocks: false });
    fireEvent.click(screen.getByText("toggle-overview"));
    expect(panesText()).toBe("ember,editor,overview");
    expect(addressText()).toBe("/workflows/wpid_1/studio");
  });
});

// Mirrors Workspace's mount-effect wiring for the handoff into the studio shell:
// open the Ember pane once when a seeded prompt lands. `threadState` verifies
// that the pane hook retains an existing handoff when a caller omits state.
function HandoffProbe({
  embedded = true,
  hasInitialEmberMessage = true,
  threadState = true,
}: {
  embedded?: boolean;
  hasInitialEmberMessage?: boolean;
  threadState?: boolean;
}) {
  const location = useLocation();
  const { panes, openPane } = useStudioPanes();
  const emberPaneOpen = panes.includes("ember");
  const emberMessage = (location.state as { emberMessage?: string } | null)
    ?.emberMessage;
  useMountEffect(() => {
    if (
      shouldOpenEmberPaneForHandoff({
        embedded,
        hasInitialEmberMessage,
        emberPaneOpen,
      })
    ) {
      openPane("ember", threadState ? { state: location.state } : undefined);
    }
  });
  return (
    <div>
      <output data-testid="panes">{panes.join(",")}</output>
      <output data-testid="copilot-message">{emberMessage ?? ""}</output>
    </div>
  );
}

function renderHandoff(
  props: {
    embedded?: boolean;
    hasInitialEmberMessage?: boolean;
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

describe("Discover → Studio handoff opens the Ember pane", () => {
  test("a seeded handoff retains the default Ember and Browser", () => {
    renderHandoff();
    expect(panesText()).toBe("ember,browser");
  });

  test("no handoff prompt leaves the default panes untouched", () => {
    renderHandoff({ hasInitialEmberMessage: false });
    expect(panesText()).toBe("ember,browser");
  });

  test("an explicit ?panes=ember handoff is left as-is (no duplicate open)", () => {
    renderHandoff(
      {},
      {
        pathname: "/workflows/wpid_1/studio",
        search: "?via=discover&panes=ember",
      },
    );
    expect(panesText()).toBe("ember");
  });

  test("the pane-open navigation preserves the handoff route state (CTA has no sessionStorage fallback)", () => {
    renderHandoff(
      {},
      {
        pathname: "/workflows/wpid_1/studio",
        search: "?panes=browser",
        state: { emberMessage: "Fill out the contact form" },
      },
    );
    expect(panesText()).toBe("browser,ember");
    expect(messageText()).toBe("Fill out the contact form");
  });

  test("retains a seeded prompt when the pane caller omits route state", () => {
    renderHandoff(
      { threadState: false },
      {
        pathname: "/workflows/wpid_1/studio",
        search: "?panes=browser",
        state: { emberMessage: "Fill out the contact form" },
      },
    );
    expect(panesText()).toBe("browser,ember");
    expect(messageText()).toBe("Fill out the contact form");
  });
});

describe("narrow-viewport clamp of shared links", () => {
  const FOUR_PANES =
    "/workflows/wpid_1/studio?panes=ember,editor,browser,overview";

  test("an over-wide shared link degrades to its fitting prefix", () => {
    renderStudio({ path: FOUR_PANES, stageWidth: 600 });
    expect(panesText()).toBe("ember,editor");
  });

  test("a wide viewport presents the shared link untouched", () => {
    renderStudio({ path: FOUR_PANES, stageWidth: 2000 });
    expect(panesText()).toBe("ember,editor,browser,overview");
  });

  test("without a measurable stage the list is presented as-is", () => {
    renderStudio({ path: FOUR_PANES });
    expect(panesText()).toBe("ember,editor,browser,overview");
  });

  test("the first pane write clears the clamp and builds on what is shown", () => {
    renderStudio({ path: FOUR_PANES, stageWidth: 600 });
    fireEvent.click(screen.getByText("toggle-overview"));
    expect(panesText()).toBe("ember,editor,overview");
  });

  test("live panes include Ember from the clamped shared selection", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?panes=ember,editor,browser",
      stageWidth: 600,
    });
    expect(panesText()).toBe("ember,editor");
    expect(livePanesText()).toBe("ember,editor");
  });

  test("clamps a deleted workflow without restoring blocked Ember", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?wr=wr_1",
      stageWidth: 500,
      workflowDeletedAt: "2026-08-12T00:00:00Z",
    });
    expect(panesText()).toBe("overview");
    expect(livePanesText()).toBe("overview");
  });

  test("an Ember-only close does not reveal a clamp-hidden run pane", () => {
    const address = "/workflows/wpid_1/studio?panes=ember,overview#proof";
    renderStudio({ path: address, stageWidth: 300 });
    expect(panesText()).toBe("ember");

    fireEvent.click(screen.getByText("toggle-copilot"));
    expect(panesText()).toBe("");
    expect(addressText()).toBe(address);

    fireEvent.click(screen.getByText("toggle-copilot"));
    expect(panesText()).toBe("ember");
    expect(addressText()).toBe(address);
  });

  test("an Ember-only reorder does not reveal a clamp-hidden run pane", () => {
    const address =
      "/workflows/wpid_1/studio?panes=ember,editor,overview#proof";
    renderStudio({ path: address, stageWidth: 600 });
    expect(panesText()).toBe("ember,editor");

    fireEvent.click(screen.getByText("reorder-copilot"));

    expect(panesText()).toBe("editor,ember");
    expect(addressText()).toBe(address);
  });

  test("an exact override removes clamp-hidden non-Ember panes", () => {
    const address =
      "/workflows/wpid_1/studio?panes=ember,editor,overview#proof";
    renderStudio({ path: address, stageWidth: 600 });
    expect(panesText()).toBe("ember,editor");

    fireEvent.click(screen.getByText("show-editor-only"));

    expect(panesText()).toBe("editor");
    expect(addressText()).toBe(address);
  });
});

describe("narrow-viewport nudge", () => {
  test("opening a pane past the min-width budget nudges exactly once", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?panes=ember,editor",
      stageWidth: 600,
    });
    fireEvent.click(screen.getByText("toggle-overview"));
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect(panesText()).toBe("ember,editor,overview");
    fireEvent.click(screen.getByText("open-browser"));
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect(useStudioFirstRunStore.getState().narrowNudgeSeen).toBe(true);
  });

  test("no nudge when the opened pane still fits", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?panes=ember",
      stageWidth: 2000,
    });
    fireEvent.click(screen.getByText("open-browser"));
    expect(toastMock).not.toHaveBeenCalled();
  });

  test("closing a pane never nudges", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?panes=ember,overview",
      stageWidth: 600,
    });
    fireEvent.click(screen.getByText("toggle-overview"));
    expect(panesText()).toBe("ember");
    expect(toastMock).not.toHaveBeenCalled();
  });
});

// Mirrors the Ember credential card: the sign-in starts, and a pane showing a
// past run is sent back to the live browser by dropping ?wr=.
function SignInProbe() {
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <button
      onClick={() => {
        useManualSignInStore.getState().setBrowserSessionId("pbs_1");
        const params = new URLSearchParams(location.search);
        params.delete("wr");
        navigate(
          { pathname: location.pathname, search: params.toString() },
          { replace: true },
        );
      }}
    >
      start-sign-in
    </button>
  );
}

describe("Ember manual sign-in", () => {
  test("opens the Browser pane on the Edit layout", () => {
    renderStudio();
    expect(panesText()).toBe("ember,editor");
    act(() => {
      useManualSignInStore.getState().setBrowserSessionId("pbs_1");
    });
    expect(panesText()).toBe("ember,editor,browser");
  });

  test("ignores a previous agent's sign-in still in the store at mount", () => {
    useManualSignInStore.getState().setBrowserSessionId("pbs_previous_agent");
    renderStudio();
    expect(panesText()).toBe("ember,editor");
    act(() => {
      useManualSignInStore.getState().setBrowserSessionId(null);
    });
    expect(panesText()).toBe("ember,editor");
  });

  test("keeps the Browser pane open when the sign-in leaves a past run", () => {
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_1/studio?wr=wr_1"]}>
        <StudioWorkflowDeletedContext.Provider value={null}>
          <StudioPaneDefaultsProvider hasBlocks={true}>
            <SignInProbe />
            <PanesProbe />
          </StudioPaneDefaultsProvider>
        </StudioWorkflowDeletedContext.Provider>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("start-sign-in"));
    expect(addressText()).toBe("/workflows/wpid_1/studio");
    expect(panesText().split(",")).toContain("browser");
  });

  // Ember + Editor + Browser needs 868px at min widths; the clamp keeps a
  // leading prefix, so Browser must not sit last.
  test("keeps the Browser pane through a narrow-stage re-resolve", () => {
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_1/studio?wr=wr_1"]}>
        <StudioWorkflowDeletedContext.Provider value={null}>
          <StudioPaneDefaultsProvider hasBlocks={true}>
            <StageProbe width={800} />
            <SignInProbe />
            <PanesProbe />
          </StudioPaneDefaultsProvider>
        </StudioWorkflowDeletedContext.Provider>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("start-sign-in"));
    expect(addressText()).toBe("/workflows/wpid_1/studio");
    expect(panesText()).toBe("ember,browser");
  });

  test("catches a sign-in a child writes in its first passive effect", () => {
    function SignInOnMount() {
      useEffect(() => {
        useManualSignInStore.getState().setBrowserSessionId("pbs_1");
      }, []);
      return null;
    }
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_1/studio"]}>
        <StudioWorkflowDeletedContext.Provider value={null}>
          <StudioPaneDefaultsProvider hasBlocks={true}>
            <SignInOnMount />
            <PanesProbe />
          </StudioPaneDefaultsProvider>
        </StudioWorkflowDeletedContext.Provider>
      </MemoryRouter>,
    );
    expect(panesText()).toBe("ember,editor,browser");
  });
});

// Mirrors the Ember chat focusing the test run its turn just started.
function EmberRunProbe() {
  const switchRun = useSwitchStudioRun({ replace: true, systemFocus: true });
  const userSwitchRun = useSwitchStudioRun();
  const { reopenEditor } = useStudioPaneDefaults();
  return (
    <>
      <button onClick={() => switchRun("wr_1")}>copilot-run-started</button>
      <button onClick={() => userSwitchRun("wr_2")}>user-opens-run</button>
      <button onClick={() => reopenEditor()}>copilot-run-ended</button>
    </>
  );
}

// The Workspace's Ember update entry, on an agent that opened with no blocks.
function EmberDraftProbe() {
  const onWorkflowUpdate = useWorkspaceEmberUpdate({
    applyWorkflowUpdate: () => true,
    embedded: true,
    initialBlockCount: 0,
  });
  const draft = (blocks: number) =>
    ({
      workflow_permanent_id: "wpid_1",
      workflow_definition: {
        blocks: Array.from({ length: blocks }, () => ({})),
      },
    }) as unknown as WorkflowVersion;
  return (
    <>
      <button
        onClick={() => onWorkflowUpdate(draft(0), { midTurnDraft: true })}
      >
        empty-draft-lands
      </button>
      <button
        onClick={() => onWorkflowUpdate(draft(2), { midTurnDraft: true })}
      >
        draft-lands
      </button>
    </>
  );
}

describe("first Ember draft on a blank agent", () => {
  test("opens Editor once the draft has blocks", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?via=discover",
      hasBlocks: false,
      children: <EmberDraftProbe />,
    });
    expect(panesText()).toBe("ember,browser");
    fireEvent.click(screen.getByText("empty-draft-lands"));
    expect(panesText()).toBe("ember,browser");
    fireEvent.click(screen.getByText("draft-lands"));
    expect(panesText()).toBe("ember,browser,editor");
  });

  test("leaves a run the user is inspecting alone when the draft lands", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?via=discover",
      hasBlocks: false,
      children: (
        <>
          <EmberRunProbe />
          <EmberDraftProbe />
        </>
      ),
    });
    fireEvent.click(screen.getByText("user-opens-run"));
    expect(panesText()).toBe("overview,browser");
    fireEvent.click(screen.getByText("draft-lands"));
    expect(panesText()).toBe("overview,browser");
  });

  test("fires once, so a later draft leaves a user's close alone", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?via=discover",
      hasBlocks: false,
      children: <EmberDraftProbe />,
    });
    fireEvent.click(screen.getByText("draft-lands"));
    fireEvent.click(screen.getByText("close-editor"));
    fireEvent.click(screen.getByText("draft-lands"));
    expect(panesText()).toBe("ember,browser");
  });
});

describe("Ember test run", () => {
  test("opens the Browser pane on the Edit layout", () => {
    renderStudio({ children: <EmberRunProbe /> });
    expect(panesText()).toBe("ember,editor");
    fireEvent.click(screen.getByText("copilot-run-started"));
    expect(addressText()).toBe("/workflows/wpid_1/studio?wr=wr_1&wrs=ember");
    expect(panesText()).toBe("ember,editor,browser");
  });

  // #18330's narrow-stage rule: Ember + Editor + Browser needs 868px at min
  // widths, so the run's Browser displaces Editor instead of cramping all three.
  test("keeps Ember + Browser on a narrow stage", () => {
    renderStudio({ stageWidth: 800, children: <EmberRunProbe /> });
    expect(panesText()).toBe("ember,editor");
    fireEvent.click(screen.getByText("copilot-run-started"));
    expect(panesText()).toBe("ember,browser");
    expect(toastMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("open-editor"));
    expect(panesText()).toBe("ember,editor,browser");
  });

  test("reopens Editor in its slot when the run ends on a narrow stage", () => {
    renderStudio({ stageWidth: 800, children: <EmberRunProbe /> });
    fireEvent.click(screen.getByText("copilot-run-started"));
    expect(panesText()).toBe("ember,browser");
    fireEvent.click(screen.getByText("copilot-run-ended"));
    expect(panesText()).toBe("ember,editor");
    expect(toastMock).not.toHaveBeenCalled();
  });

  test("opens Editor on a blank agent when the run ends", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?via=discover",
      hasBlocks: false,
      children: <EmberRunProbe />,
    });
    expect(panesText()).toBe("ember,browser");
    fireEvent.click(screen.getByText("copilot-run-ended"));
    expect(panesText()).toBe("ember,browser,editor");
  });

  test("shows Editor on a stage too narrow for Ember beside it", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?via=discover",
      hasBlocks: false,
      stageWidth: 400,
      children: <EmberRunProbe />,
    });
    expect(panesText()).toBe("ember");
    fireEvent.click(screen.getByText("copilot-run-ended"));
    expect(panesText()).toBe("editor");
  });

  test("leaves a Browser-only layout the user chose mid-run alone", () => {
    renderStudio({ stageWidth: 800, children: <EmberRunProbe /> });
    fireEvent.click(screen.getByText("copilot-run-started"));
    expect(panesText()).toBe("ember,browser");
    fireEvent.click(screen.getByText("user-shows-browser-only"));
    fireEvent.click(screen.getByText("copilot-run-ended"));
    expect(panesText()).toBe("browser");
  });

  test("leaves Editor closed when the user closed it during the session", () => {
    renderStudio({ children: <EmberRunProbe /> });
    fireEvent.click(screen.getByText("copilot-run-started"));
    fireEvent.click(screen.getByText("toggle-editor"));
    expect(panesText()).toBe("ember,browser");
    fireEvent.click(screen.getByText("copilot-run-ended"));
    expect(panesText()).toBe("ember,browser");
  });

  test("leaves Editor closed after the user closes it with the pane's close button", () => {
    renderStudio({ children: <EmberRunProbe /> });
    fireEvent.click(screen.getByText("close-editor"));
    fireEvent.click(screen.getByText("copilot-run-started"));
    fireEvent.click(screen.getByText("copilot-run-ended"));
    expect(panesText()).toBe("ember,browser");
  });

  test("reopens Editor again once anything has reopened it since the user's close", () => {
    renderStudio({ stageWidth: 800, children: <EmberRunProbe /> });
    fireEvent.click(screen.getByText("toggle-editor"));
    fireEvent.click(screen.getByText("open-editor"));
    fireEvent.click(screen.getByText("copilot-run-started"));
    expect(panesText()).toBe("ember,browser");
    fireEvent.click(screen.getByText("copilot-run-ended"));
    expect(panesText()).toBe("ember,editor");
  });

  test("keeps a user's Editor close across a run they switch to", () => {
    renderStudio({ children: <EmberRunProbe /> });
    fireEvent.click(screen.getByText("toggle-editor"));
    fireEvent.click(screen.getByText("copilot-run-started"));
    fireEvent.click(screen.getByText("user-opens-run"));
    expect(panesText()).toBe("overview,browser");
    fireEvent.click(screen.getByText("copilot-run-ended"));
    expect(panesText()).toBe("overview,browser");
  });

  test("leaves a cramped layout with Browser already open alone", () => {
    renderStudio({ stageWidth: 800, children: <EmberRunProbe /> });
    fireEvent.click(screen.getByText("open-browser"));
    expect(panesText()).toBe("ember,editor,browser");
    fireEvent.click(screen.getByText("copilot-run-started"));
    expect(panesText()).toBe("ember,editor,browser");
  });

  test.each([
    [600, "ember,browser"],
    [400, "browser"],
  ])(
    "keeps Browser when other panes fill a %ipx stage",
    (stageWidth, expected) => {
      renderStudio({
        path: "/workflows/wpid_1/studio?panes=ember,overview",
        stageWidth,
        children: <EmberRunProbe />,
      });
      fireEvent.click(screen.getByText("copilot-run-started"));
      expect(panesText()).toBe(expected);
    },
  );

  test("displaces only Editor from a reordered narrow layout", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?panes=editor,ember",
      stageWidth: 800,
      children: <EmberRunProbe />,
    });
    fireEvent.click(screen.getByText("copilot-run-started"));
    expect(panesText()).toBe("ember,browser");
    fireEvent.click(screen.getByText("open-editor"));
    expect(panesText()).toBe("editor,ember,browser");
  });

  test("keeps a pane order the user arranged", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?panes=editor,ember",
      children: <EmberRunProbe />,
    });
    fireEvent.click(screen.getByText("copilot-run-started"));
    expect(panesText()).toBe("editor,ember,browser");
  });

  test("leaves an open Browser pane where it is", () => {
    renderStudio({
      path: "/workflows/wpid_1/studio?panes=browser,ember",
      children: <EmberRunProbe />,
    });
    fireEvent.click(screen.getByText("copilot-run-started"));
    expect(panesText()).toBe("browser,ember");
  });

  test("a run during a narrow-stage sign-in keeps the sign-in layout", () => {
    renderStudio({ stageWidth: 800, children: <EmberRunProbe /> });
    act(() => {
      useManualSignInStore.getState().setBrowserSessionId("pbs_1");
    });
    const signInPanes = panesText();
    expect(signInPanes.split(",")).toContain("browser");
    fireEvent.click(screen.getByText("copilot-run-started"));
    expect(panesText()).toBe(signInPanes);
  });

  test("a reload of an Ember-focused run keeps the Edit panes", () => {
    renderStudio({ path: "/workflows/wpid_1/studio?wr=wr_1&wrs=ember" });
    expect(panesText()).toBe("ember,editor");
  });
});
