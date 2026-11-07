// @vitest-environment jsdom

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { TooltipProvider } from "@/components/ui/tooltip";
import { type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useCopilotHeaderStore } from "@/store/useCopilotHeaderStore";
import { useManualSignInStore } from "@/store/useManualSignInStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { type StudioWorkspaceProps } from "./EditorTab";
import { type StudioPaneId } from "./panes";
import { paneAccessibleName } from "./paneMeta";
import { paneExpansionKeyframes } from "./paneLayout";
import {
  EmbeddedBrowserOverlays,
  StudioPane,
  StudioShell,
} from "./StudioShell";

vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn(), captureException: vi.fn() }),
}));
vi.mock("@/hooks/useLogging", () => ({
  useLogging: () => ({ error: vi.fn() }),
}));
vi.mock("./BrowserPaneHeader", () => ({
  BrowserPaneActions: () => null,
  BrowserPaneViewPills: () => null,
}));
vi.mock("./CopilotPaneHeader", () => ({
  CopilotActiveDot: () => null,
  CopilotPaneControls: () => null,
  CopilotPaneStatus: () => null,
}));
vi.mock("./EditorPaneHeader", () => ({
  EditorPaneBlockSearch: () => null,
  EditorPaneModeToggle: () => null,
}));
// Drives the Doc fold without real layout: jsdom measures every stage as 0px.
const docFold = vi.hoisted(() => ({ folded: [] as StudioPaneId[] }));
vi.mock("./useEditorShowsDoc", () => ({
  useEditorShowsDoc: () => docFold.folded.length > 0,
}));
vi.mock("./panes", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./panes")>();
  return {
    ...actual,
    panesCollapsedForDoc: (panes: readonly StudioPaneId[]) =>
      docFold.folded.filter((id) => panes.includes(id)),
  };
});
vi.mock("./BrowserTab", () => ({ BrowserTab: () => null }));
vi.mock("./EditorTab", () => ({ EditorTab: () => null }));
vi.mock("./RunTab", () => ({ RunTab: () => null }));
vi.mock("./runview/RunPaneHeader", () => ({
  RunPaneActions: () => null,
  RunPaneViewToggles: () => null,
}));
vi.mock("./StudioBrowserStream", () => ({ StudioBrowserStream: () => null }));
vi.mock("./StudioStageLauncher", () => ({ StudioStageLauncher: () => null }));
// Stand-ins for controls that ask to see a pane that may already be open: the
// top bar's Past Runs pick and the Run pane's "watch live".
vi.mock("./StudioTopBar", async () => {
  const { useStudioPanes } = await import("./useStudioPanes");
  return {
    StudioTopBar: function OpenPaneRequests() {
      const { openPane } = useStudioPanes();
      return (
        <>
          <button type="button" onClick={() => openPane("overview")}>
            Pick another run
          </button>
          <button type="button" onClick={() => openPane("browser")}>
            Watch live
          </button>
        </>
      );
    },
  };
});
vi.mock("./StudioWorkflowPanels", () => ({ StudioWorkflowPanels: () => null }));

vi.mock("./useRunVisuals", () => ({
  useRunVisuals: () => ({
    recordingArchived: true,
    recordingUrls: [],
  }),
}));
vi.mock("../workflowRun/WorkflowRunVerificationCodeForm", () => ({
  WorkflowRunVerificationCodeForm: ({
    workflowRunId,
  }: {
    workflowRunId: string | undefined;
  }) => <div data-testid="verification-form">{workflowRunId}</div>,
}));

describe("EmbeddedBrowserOverlays", () => {
  test("keeps verification available in screenshot-only browser views", () => {
    render(
      <EmbeddedBrowserOverlays runId="wr_1" showArchivedRecording={false} />,
    );

    expect(screen.getByTestId("verification-form").textContent).toBe("wr_1");
    expect(screen.queryByText("Recording archived")).toBeNull();
  });
});

describe("StudioShell inspected run header", () => {
  test("keeps the run pane header plain when a run is inspected", () => {
    const runId = "wr_synthetic_inspected_run";
    const props = {
      workflow: {
        workflow_permanent_id: "wpid_synthetic",
        organization_id: "org_synthetic",
        deleted_at: null,
        workflow_definition: { blocks: [] },
      },
      initialTitle: "Synthetic workflow",
    } as unknown as StudioWorkspaceProps;

    render(
      <MemoryRouter
        initialEntries={[`/workflows/wpid_synthetic/studio?wr=${runId}`]}
      >
        <StudioShell {...props} />
      </MemoryRouter>,
    );

    const header = screen.getByRole("group", { name: "Run pane header" });
    expect(within(header).getByText("Run", { exact: true })).toBeTruthy();
    expect(header.textContent).toBe("Run");
    expect(
      within(header).queryByRole("button", { name: "Copy to clipboard" }),
    ).toBeNull();
  });
});

describe("StudioShell with a pane folded for the Doc", () => {
  afterEach(() => {
    docFold.folded = [];
  });

  const props = {
    workflow: {
      workflow_permanent_id: "wpid_synthetic",
      organization_id: "org_synthetic",
      deleted_at: null,
      workflow_definition: { blocks: [] },
    },
    initialTitle: "Synthetic workflow",
  } as unknown as StudioWorkspaceProps;
  const shown = () =>
    screen
      .getAllByRole("region")
      .filter((pane) => !pane.classList.contains("hidden"))
      .map((pane) => pane.getAttribute("aria-label"));

  test("asking to see a folded pane opens it over the stage", () => {
    docFold.folded = ["browser"];
    render(
      <MemoryRouter
        initialEntries={[
          "/workflows/wpid_synthetic/studio?panes=editor,browser&wr=wr_synthetic_a",
        ]}
      >
        <StudioShell {...props} />
      </MemoryRouter>,
    );
    expect(shown()).not.toContain("Browser");
    expect(
      screen.getByRole("button", { name: /^Show Browser pane/ }),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Watch live" }));

    expect(shown()).toEqual(["Browser"]);
  });

  const renderDoc = (panes: string) =>
    render(
      <MemoryRouter
        initialEntries={[
          `/workflows/wpid_synthetic/studio?panes=${panes}&wr=wr_synthetic_a`,
        ]}
      >
        <StudioShell {...props} />
      </MemoryRouter>,
    );

  test("a closed pane that folds as it opens is shown, not left a strip", () => {
    docFold.folded = ["browser"];
    renderDoc("editor");
    expect(shown()).not.toContain("Browser");

    fireEvent.click(screen.getByRole("button", { name: "Watch live" }));

    expect(shown()).toEqual(["Browser"]);
  });

  test("signing in yourself shows a folded Browser", () => {
    docFold.folded = ["browser"];
    renderDoc("editor,browser");
    expect(shown()).not.toContain("Browser");
    try {
      act(() => {
        useManualSignInStore.setState({ browserSessionId: "pbs_synthetic" });
      });
      expect(shown()).toEqual(["Browser"]);
    } finally {
      act(() => {
        useManualSignInStore.setState({ browserSessionId: null });
      });
    }
  });

  test("a folded Browser's strip flags new activity", () => {
    docFold.folded = ["browser"];
    useStudioBrowserStore.setState({ hasUnseenActivity: true });
    try {
      renderDoc("editor,browser");
      expect(
        screen.getByRole("button", {
          name: /^Show Browser pane .*, new activity$/,
        }),
      ).toBeTruthy();
    } finally {
      useStudioBrowserStore.setState({ hasUnseenActivity: false });
    }
  });

  test("keyboard focus follows a folded pane as it opens and folds again", () => {
    docFold.folded = ["browser"];
    renderDoc("editor,browser");
    fireEvent.click(screen.getByRole("button", { name: /^Show Browser pane/ }));
    expect(shown()).toEqual(["Browser"]);
    const browser = screen.getByRole("region", { name: "Browser" });
    expect(browser.contains(document.activeElement)).toBe(true);

    fireEvent.click(
      within(browser).getByRole("button", { name: "Restore Browser pane" }),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: /^Show Browser pane/ }),
    );
  });
});

describe("StudioShell at phone width", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const renderPhone = (path = "/workflows/wpid_synthetic/studio") => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: query === "(max-width: 719px)",
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    const props = {
      workflow: {
        workflow_permanent_id: "wpid_synthetic",
        organization_id: "org_synthetic",
        deleted_at: null,
        workflow_definition: { blocks: [] },
      },
      initialTitle: "Synthetic workflow",
    } as unknown as StudioWorkspaceProps;
    render(
      <MemoryRouter initialEntries={[path]}>
        <StudioShell {...props} />
      </MemoryRouter>,
    );
  };
  const shown = () =>
    screen
      .getAllByRole("region")
      .filter((pane) => !pane.classList.contains("hidden"))
      .map((pane) => pane.getAttribute("aria-label"));
  const paneSwitch = () =>
    screen.getByRole("navigation", { name: "Pane switch" });

  test("shows one pane at a time and the pane switch moves between them", () => {
    renderPhone();
    expect(shown()).toEqual(["Copilot"]);
    expect(
      within(paneSwitch())
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["Copilot", "Editor", "Browser"]);

    fireEvent.click(
      within(paneSwitch()).getByRole("button", { name: "Editor" }),
    );
    expect(shown()).toEqual(["Editor"]);

    fireEvent.click(
      within(paneSwitch()).getByRole("button", { name: "Browser" }),
    );
    expect(shown()).toEqual(["Browser"]);
    expect(
      within(paneSwitch())
        .getByRole("button", { name: "Browser" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
  });

  test("picking another run while Run is already open shows Run", () => {
    renderPhone("/workflows/wpid_synthetic/studio?wr=wr_synthetic_a");
    fireEvent.click(
      within(paneSwitch()).getByRole("button", { name: "Browser" }),
    );
    expect(shown()).toEqual(["Browser"]);

    fireEvent.click(screen.getByRole("button", { name: "Pick another run" }));
    expect(shown()).toEqual(["Run"]);
  });

  test("the pane switch flags Browser activity and a waiting Copilot on panes not shown", () => {
    useStudioBrowserStore.setState({ hasUnseenActivity: true });
    useCopilotHeaderStore.setState({ attention: "question" });
    try {
      renderPhone();
      expect(
        within(paneSwitch()).getByRole("button", {
          name: "Browser, new activity",
        }),
      ).toBeTruthy();
      // Copilot is the shown pane, so it carries no dot yet.
      expect(
        within(paneSwitch()).getByRole("button", { name: "Copilot" }),
      ).toBeTruthy();

      fireEvent.click(
        within(paneSwitch()).getByRole("button", { name: "Editor" }),
      );
      expect(
        within(paneSwitch()).getByRole("button", {
          name: "Copilot, waiting for your answer",
        }),
      ).toBeTruthy();
    } finally {
      useStudioBrowserStore.setState({ hasUnseenActivity: false });
      useCopilotHeaderStore.setState({ attention: null });
    }
  });

  test("signing in yourself shows a Browser open behind another pane", () => {
    renderPhone("/workflows/wpid_synthetic/studio?panes=copilot,browser");
    expect(shown()).toEqual(["Copilot"]);
    try {
      act(() => {
        useManualSignInStore.setState({ browserSessionId: "pbs_synthetic" });
      });
      expect(shown()).toEqual(["Browser"]);
    } finally {
      act(() => {
        useManualSignInStore.setState({ browserSessionId: null });
      });
    }
  });

  test("watching live while Browser is open behind Run shows Browser", () => {
    renderPhone("/workflows/wpid_synthetic/studio?wr=wr_synthetic_a");
    fireEvent.click(
      within(paneSwitch()).getByRole("button", { name: "Browser" }),
    );
    fireEvent.click(within(paneSwitch()).getByRole("button", { name: "Run" }));
    expect(shown()).toEqual(["Run"]);

    fireEvent.click(screen.getByRole("button", { name: "Watch live" }));
    expect(shown()).toEqual(["Browser"]);
  });

  test("closing the shown pane moves focus to the pane switch's shown item", () => {
    renderPhone("/workflows/wpid_synthetic/studio?panes=browser,editor");
    fireEvent.click(
      within(paneSwitch()).getByRole("button", { name: "Editor" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Close Editor pane" }));

    expect(shown()).toEqual(["Browser"]);
    expect(document.activeElement).toBe(
      within(paneSwitch()).getByRole("button", { name: "Browser" }),
    );
  });
});

describe("paneExpansionKeyframes", () => {
  test("grows from the pane's current bounds into its final bounds", () => {
    expect(
      paneExpansionKeyframes(
        { left: 260, top: 100, width: 300, height: 500 },
        { left: 20, top: 40, width: 1000, height: 600 },
      ),
    ).toEqual([
      {
        transform: "translate(240px, 60px) scale(0.3, 0.8333333333333334)",
        transformOrigin: "top left",
      },
      { transform: "none", transformOrigin: "top left" },
    ]);
  });
});

// Chromium aborts a native drag when the DOM mutates inside the dragstart
// task, so the reorder state (drop overlays, source dim) must engage on a
// later task. These tests pin that timing contract; only a real mouse drag
// can prove the native drag itself survives.
describe("StudioPane header", () => {
  const dataTransfer = () => ({ setData: vi.fn(), effectAllowed: "" });

  const renderPane = ({
    id = "copilot",
    headerActions,
    expanded = false,
    expansionTransitioning = false,
    onToggleExpanded = vi.fn(),
    transitionFromBounds,
    onTransitionEnd,
  }: {
    id?: StudioPaneId;
    headerActions?: ReactNode;
    expanded?: boolean;
    expansionTransitioning?: boolean;
    onToggleExpanded?: () => void;
    transitionFromBounds?: {
      left: number;
      top: number;
      width: number;
      height: number;
    };
    onTransitionEnd?: () => void;
  } = {}) => {
    const reorder = {
      draggingId: null,
      placement: null,
      onStart: vi.fn(),
      onEnd: vi.fn(),
      onDrop: vi.fn(),
      onMove: vi.fn(),
    };
    render(
      <TooltipProvider delayDuration={0}>
        <StudioPane
          id={id}
          open
          order={0}
          flex={undefined}
          reorder={reorder}
          onClose={vi.fn()}
          expanded={expanded}
          expansionTransitioning={expansionTransitioning}
          onToggleExpanded={onToggleExpanded}
          transitionFromBounds={transitionFromBounds}
          onTransitionEnd={onTransitionEnd}
          headerActions={headerActions}
        >
          <div>content</div>
        </StudioPane>
      </TooltipProvider>,
    );
    return {
      reorder,
      pane: screen.getByRole("region", { name: paneAccessibleName(id) }),
      header: screen.getByRole("group", {
        name: `${paneAccessibleName(id)} pane header`,
      }),
    };
  };

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    Reflect.deleteProperty(HTMLElement.prototype, "animate");
  });

  test("gives each pane a clear two-pixel outline", () => {
    const { pane } = renderPane();

    expect(pane.className).toContain("border-2");
    expect(pane.className).toContain("border-border");
  });

  test("expands a pane to the full studio stage from its header", () => {
    const onToggleExpanded = vi.fn();
    const { pane } = renderPane({ onToggleExpanded });
    const expand = screen.getByRole("button", {
      name: "Expand Copilot pane",
    });

    expect(expand.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(expand);

    expect(onToggleExpanded).toHaveBeenCalledTimes(1);
    expect(pane.className).not.toContain("absolute");
  });

  test("restores an expanded pane to the multi-pane layout", () => {
    const onToggleExpanded = vi.fn();
    const { pane } = renderPane({ expanded: true, onToggleExpanded });
    const restore = screen.getByRole("button", {
      name: "Restore Copilot pane",
    });

    expect(restore.getAttribute("aria-pressed")).toBe("true");
    expect(pane.className).toContain("absolute");
    expect(pane.className).toContain("inset-3");
    fireEvent.click(restore);

    expect(onToggleExpanded).toHaveBeenCalledTimes(1);
  });

  test("prevents another fullscreen toggle during the transition", () => {
    const onToggleExpanded = vi.fn();
    renderPane({ expansionTransitioning: true, onToggleExpanded });
    const expand = screen.getByRole("button", {
      name: "Expand Copilot pane",
    });

    expect(expand.getAttribute("aria-disabled")).toBe("true");
    expect((expand as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(expand);
    expect(onToggleExpanded).not.toHaveBeenCalled();
  });

  test("animates from the measured pane bounds over 200ms", () => {
    const animation = {
      cancel: vi.fn(),
      onfinish: null,
    } as unknown as Animation;
    const animate = vi.fn(() => animation);
    Object.defineProperty(HTMLElement.prototype, "animate", {
      configurable: true,
      value: animate,
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 20,
      top: 40,
      width: 1000,
      height: 600,
    } as DOMRect);
    const onTransitionEnd = vi.fn();

    renderPane({
      transitionFromBounds: {
        left: 260,
        top: 100,
        width: 300,
        height: 500,
      },
      onTransitionEnd,
    });

    expect(animate).toHaveBeenCalledWith(
      paneExpansionKeyframes(
        { left: 260, top: 100, width: 300, height: 500 },
        { left: 20, top: 40, width: 1000, height: 600 },
      ),
      {
        duration: 200,
        easing: "cubic-bezier(0.22, 1, 0.36, 1)",
        fill: "both",
      },
    );
    animation.onfinish?.(new Event("finish") as AnimationPlaybackEvent);
    expect(onTransitionEnd).toHaveBeenCalledTimes(1);
  });

  test("finishes immediately when reduced motion is preferred", () => {
    const animate = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "animate", {
      configurable: true,
      value: animate,
    });
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({ matches: true })),
    );
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 20,
      top: 40,
      width: 1000,
      height: 600,
    } as DOMRect);
    const onTransitionEnd = vi.fn();

    renderPane({
      transitionFromBounds: {
        left: 260,
        top: 100,
        width: 300,
        height: 500,
      },
      onTransitionEnd,
    });

    expect(animate).not.toHaveBeenCalled();
    expect(onTransitionEnd).toHaveBeenCalledTimes(1);
  });

  test("dragstart sets the drag payload synchronously but engages reorder on a later task", () => {
    const { reorder, header } = renderPane();
    const dt = dataTransfer();

    fireEvent.dragStart(header, { dataTransfer: dt });

    expect(dt.setData).toHaveBeenCalledWith(
      "application/x-skyvern-studio-pane",
      "copilot",
    );
    expect(reorder.onStart).not.toHaveBeenCalled();

    vi.runAllTimers();
    expect(reorder.onStart).toHaveBeenCalledTimes(1);
  });

  test("a drag cancelled before it engages never turns the reorder state on", () => {
    const { reorder, header } = renderPane();

    fireEvent.dragStart(header, { dataTransfer: dataTransfer() });
    fireEvent.dragEnd(header);

    vi.runAllTimers();
    expect(reorder.onStart).not.toHaveBeenCalled();
    expect(reorder.onEnd).toHaveBeenCalledTimes(1);
  });

  test("a drag starting on a header button is prevented", () => {
    const { reorder, header } = renderPane();

    fireEvent.pointerDown(
      screen.getByRole("button", { name: "Close Copilot pane" }),
    );
    const notPrevented = fireEvent.dragStart(header, {
      dataTransfer: dataTransfer(),
    });

    expect(notPrevented).toBe(false);
    vi.runAllTimers();
    expect(reorder.onStart).not.toHaveBeenCalled();
  });

  test("the run pane header stays plain and has no copy action", () => {
    renderPane({ id: "overview" });

    expect(screen.getByText("Run")).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Copy to clipboard" }),
    ).toBeNull();
  });

  test("groups pane utilities separately from close", () => {
    const { header } = renderPane({
      id: "browser",
      headerActions: (
        <>
          <button type="button">Reconnect</button>
          <button type="button">Open in new tab</button>
        </>
      ),
    });

    const actions = header.querySelector("[data-pane-header-actions]");
    expect(actions).not.toBeNull();
    if (!actions) {
      throw new Error("pane action cluster was not rendered");
    }
    expect(
      actions.contains(screen.getByRole("button", { name: "Reconnect" })),
    ).toBe(true);
    expect(
      actions.contains(screen.getByRole("button", { name: "Open in new tab" })),
    ).toBe(true);
    const close = screen.getByRole("button", { name: "Close Browser pane" });
    expect(actions.contains(close)).toBe(false);
  });
});
