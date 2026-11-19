// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { StudioPaneDefaultsProvider } from "./StudioPaneDefaults";
import { type StudioPaneId } from "./panes";
import { StudioShellContext } from "./StudioShellContext";
import { useStudioPanes } from "./useStudioPanes";

beforeEach(() => {
  localStorage.clear();
});

function OrderProbe({ order }: { order: StudioPaneId[] }) {
  const { panes, setPanesOrder, setOpenPanes } = useStudioPanes();
  const location = useLocation();
  return (
    <div>
      <output data-testid="panes">{panes.join(",")}</output>
      <output data-testid="search">{location.search}</output>
      <button onClick={() => setPanesOrder(order)}>set-order</button>
      <button onClick={() => setOpenPanes(order)}>set-open</button>
    </div>
  );
}

function renderWithPanes(search: string, order: StudioPaneId[]) {
  return render(
    <MemoryRouter initialEntries={[`/studio${search}`]}>
      <StudioPaneDefaultsProvider hasBlocks={true}>
        <OrderProbe order={order} />
      </StudioPaneDefaultsProvider>
    </MemoryRouter>,
  );
}

function EmberVisitProbe() {
  const { panes, togglePane, openPane, closePane, setPanesOrder } =
    useStudioPanes();
  const location = useLocation();
  const navigate = useNavigate();
  const address = `${location.pathname}${location.search}${location.hash}`;
  return (
    <div>
      <output data-testid="panes">{panes.join(",")}</output>
      <output data-testid="address">{address}</output>
      <output data-testid="route-state">
        {JSON.stringify(location.state)}
      </output>
      <button onClick={() => togglePane("ember")}>toggle-copilot</button>
      <button onClick={() => togglePane("browser")}>toggle-browser</button>
      <button onClick={() => togglePane("editor")}>toggle-editor</button>
      <button onClick={() => openPane("browser")}>open-browser</button>
      <button onClick={() => closePane("ember")}>close-copilot</button>
      <button onClick={() => openPane("ember")}>open-copilot</button>
      <button onClick={() => setPanesOrder(["ember", "browser"])}>
        drag-copilot-first
      </button>
      <button
        onClick={() =>
          openPane("ember", {
            state: { emberMessage: "Fix this run" },
          })
        }
      >
        open-copilot-with-state
      </button>
      <button
        onClick={() =>
          navigate(
            "/studio?cache-key-value=x&panes=ember,editor,browser&wr=wr_1#proof",
          )
        }
      >
        inspect-run
      </button>
      <button
        onClick={() =>
          navigate("/studio?cache-key-value=x&panes=ember,editor,browser#proof")
        }
      >
        return-to-studio
      </button>
    </div>
  );
}

function renderEmberVisit(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <StudioPaneDefaultsProvider hasBlocks={true}>
        <EmberVisitProbe />
      </StudioPaneDefaultsProvider>
    </MemoryRouter>,
  );
}

describe("useStudioPanes visit-scoped Ember selection", () => {
  test("restores an expanded pane before a nested pane action, even when the target is already open", () => {
    const restoreExpandedPane = vi.fn();
    render(
      <StudioShellContext.Provider
        value={{
          emberPortalEl: null,
          setEditorStreamSlot: vi.fn(),
          setBrowserStreamSlot: vi.fn(),
          setRunStreamSlot: vi.fn(),
          restoreExpandedPane,
        }}
      >
        <MemoryRouter initialEntries={["/studio?panes=editor,browser"]}>
          <StudioPaneDefaultsProvider hasBlocks={true}>
            <EmberVisitProbe />
          </StudioPaneDefaultsProvider>
        </MemoryRouter>
      </StudioShellContext.Provider>,
    );

    fireEvent.click(screen.getByText("open-browser"));

    expect(restoreExpandedPane).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("panes").textContent).toBe("editor,browser");
  });

  test("reapplies shared entry panes when navigating between Studio and runs", () => {
    const studioAddress =
      "/studio?cache-key-value=x&panes=ember,editor,browser#proof";
    const runAddress =
      "/studio?cache-key-value=x&panes=ember,editor,browser&wr=wr_1#proof";
    renderEmberVisit(studioAddress);

    expect(screen.getByTestId("panes").textContent).toBe(
      "ember,editor,browser",
    );
    fireEvent.click(screen.getByText("toggle-copilot"));
    expect(screen.getByTestId("panes").textContent).toBe("editor,browser");
    expect(screen.getByTestId("address").textContent).toBe(studioAddress);

    fireEvent.click(screen.getByText("inspect-run"));
    expect(screen.getByTestId("panes").textContent).toBe(
      "ember,editor,browser",
    );
    expect(screen.getByTestId("address").textContent).toBe(runAddress);

    // The run entry starts its own slot memory, so Ember reopens in place.
    fireEvent.click(screen.getByText("toggle-copilot"));
    expect(screen.getByTestId("address").textContent).toBe(runAddress);
    fireEvent.click(screen.getByText("toggle-copilot"));
    expect(screen.getByTestId("panes").textContent).toBe(
      "ember,editor,browser",
    );
    expect(screen.getByTestId("address").textContent).toBe(runAddress);

    fireEvent.click(screen.getByText("return-to-studio"));
    expect(screen.getByTestId("panes").textContent).toBe(
      "ember,editor,browser",
    );
    expect(screen.getByTestId("address").textContent).toBe(studioAddress);

    fireEvent.click(screen.getByText("inspect-run"));
    expect(screen.getByTestId("panes").textContent).toBe(
      "ember,editor,browser",
    );
    expect(screen.getByTestId("address").textContent).toBe(runAddress);
  });

  test("keeps route-state handoffs while preserving pathname, search, and hash", () => {
    const address = "/studio?cache-key-value=x&panes=editor,browser#proof";
    renderEmberVisit(address);

    fireEvent.click(screen.getByText("open-copilot-with-state"));

    expect(screen.getByTestId("panes").textContent).toBe(
      "editor,browser,ember",
    );
    expect(screen.getByTestId("address").textContent).toBe(address);
    expect(screen.getByTestId("route-state").textContent).toBe(
      '{"emberMessage":"Fix this run"}',
    );

    fireEvent.click(screen.getByText("toggle-browser"));
    expect(screen.getByTestId("panes").textContent).toBe("editor,ember");
    expect(screen.getByTestId("address").textContent).toBe(address);
    expect(screen.getByTestId("route-state").textContent).toBe(
      '{"emberMessage":"Fix this run"}',
    );
  });

  test("preserves the incoming URL through multiple pane toggles", () => {
    renderEmberVisit(
      "/studio?cache-key-value=x&panes=ember,editor,browser#proof",
    );

    fireEvent.click(screen.getByText("toggle-copilot"));
    fireEvent.click(screen.getByText("toggle-browser"));

    expect(screen.getByTestId("panes").textContent).toBe("editor");
    expect(screen.getByTestId("address").textContent).toBe(
      "/studio?cache-key-value=x&panes=ember,editor,browser#proof",
    );
  });

  test("keeps Ember order through a non-Ember pane toggle", () => {
    renderEmberVisit("/studio?panes=editor,ember,browser#proof");

    fireEvent.click(screen.getByText("toggle-editor"));

    expect(screen.getByTestId("panes").textContent).toBe("ember,browser");
    expect(screen.getByTestId("address").textContent).toBe(
      "/studio?panes=editor,ember,browser#proof",
    );
  });

  test("reopens Ember in the slot it was closed from", () => {
    const address =
      "/studio?cache-key-value=x&panes=editor,ember,browser#proof";
    renderEmberVisit(address);

    fireEvent.click(screen.getByText("toggle-copilot"));
    fireEvent.click(screen.getByText("toggle-copilot"));

    expect(screen.getByTestId("panes").textContent).toBe(
      "editor,ember,browser",
    );
    expect(screen.getByTestId("address").textContent).toBe(address);
  });

  test("reopens a dragged pane where the drag left it, not at the end", () => {
    renderEmberVisit("/studio?panes=browser,ember");

    fireEvent.click(screen.getByText("drag-copilot-first"));
    fireEvent.click(screen.getByText("close-copilot"));
    expect(screen.getByTestId("panes").textContent).toBe("browser");
    fireEvent.click(screen.getByText("open-copilot"));

    expect(screen.getByTestId("panes").textContent).toBe("ember,browser");
  });

  test("appends a pane that has not been open during this visit", () => {
    renderEmberVisit("/studio?panes=ember,browser");

    fireEvent.click(screen.getByText("toggle-editor"));

    expect(screen.getByTestId("panes").textContent).toBe(
      "ember,browser,editor",
    );
  });
});

describe("useStudioPanes setOpenPanes", () => {
  test("replaces the open set outright (layout override)", () => {
    renderWithPanes("?panes=ember,browser,overview", ["editor"]);
    fireEvent.click(screen.getByText("set-open"));
    expect(screen.getByTestId("panes").textContent).toBe("editor");
  });
});

describe("useStudioPanes setPanesOrder", () => {
  test("reorders runtime panes while preserving the URL", () => {
    renderWithPanes("?panes=ember,editor,browser", [
      "editor",
      "browser",
      "ember",
    ]);

    const search = screen.getByTestId("search").textContent;
    fireEvent.click(screen.getByText("set-order"));
    expect(screen.getByTestId("search").textContent).toBe(search);

    expect(screen.getByTestId("panes").textContent).toBe(
      "editor,browser,ember",
    );
  });

  test("keeps the current open set: closed panes in the order are dropped, missing ones appended", () => {
    // "overview" is not open, so it must not open; "browser" is open but
    // absent from the requested order, so it keeps a slot at the end.
    renderWithPanes("?panes=ember,editor,browser", [
      "overview",
      "editor",
      "ember",
    ]);

    const search = screen.getByTestId("search").textContent;
    fireEvent.click(screen.getByText("set-order"));
    expect(screen.getByTestId("search").textContent).toBe(search);

    expect(screen.getByTestId("panes").textContent).toBe(
      "editor,ember,browser",
    );
  });

  test("ignores duplicate entries in the requested order", () => {
    renderWithPanes("?panes=ember,browser", ["browser", "browser", "ember"]);

    const search = screen.getByTestId("search").textContent;
    fireEvent.click(screen.getByText("set-order"));
    expect(screen.getByTestId("search").textContent).toBe(search);

    expect(screen.getByTestId("panes").textContent).toBe("browser,ember");
  });
});

function RunRouteProbe() {
  const { panes } = useStudioPanes();
  return <output data-testid="panes">{panes.join(",")}</output>;
}

function renderAtRunRoute(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route
          path="/runs/:runId/*"
          element={
            <StudioPaneDefaultsProvider hasBlocks={true}>
              <RunRouteProbe />
            </StudioPaneDefaultsProvider>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("useStudioPanes under the short run URL", () => {
  test("/runs/{wr} opens the run layout from the path, not the edit default", () => {
    renderAtRunRoute("/runs/wr_1");
    expect(screen.getByTestId("panes").textContent).toBe("overview,browser");
  });

  test("an explicit ?panes= still wins under the short run URL", () => {
    renderAtRunRoute("/runs/wr_1?panes=ember");
    expect(screen.getByTestId("panes").textContent).toBe("ember");
  });
});

function OpenWithParamsProbe() {
  const { panes, openPane } = useStudioPanes();
  const location = useLocation();
  return (
    <div>
      <output data-testid="search">{location.search}</output>
      <output data-testid="panes">{panes.join(",")}</output>
      <button
        onClick={() =>
          openPane("ember", {
            selectedBlockLabel: "checkout",
          })
        }
      >
        open-with-params
      </button>
      <button
        onClick={() =>
          openPane("ember", {
            selectedBlockLabel: null,
          })
        }
      >
        open-clearing-params
      </button>
      <button
        onClick={() =>
          openPane("browser", {
            selectedBlockLabel: "checkout",
          })
        }
      >
        open-browser-with-params
      </button>
    </div>
  );
}

describe("useStudioPanes selectedBlockLabel", () => {
  test("openPane carries selectedBlockLabel in the same navigation", () => {
    render(
      <MemoryRouter initialEntries={["/studio?panes=editor"]}>
        <StudioPaneDefaultsProvider hasBlocks={true}>
          <OpenWithParamsProbe />
        </StudioPaneDefaultsProvider>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("open-with-params"));
    // Ember's open state is runtime-only by design, so the pane change shows
    // up in the pane list while the extra param lands in the one navigation.
    expect(screen.getByTestId("search").textContent).toContain(
      "selected-block=checkout",
    );
    expect(screen.getByTestId("panes").textContent).toContain("ember");
  });

  test("opening Browser preserves the pane URL while setting the selected block", () => {
    render(
      <MemoryRouter initialEntries={["/studio?panes=editor"]}>
        <StudioPaneDefaultsProvider hasBlocks={true}>
          <OpenWithParamsProbe />
        </StudioPaneDefaultsProvider>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("open-browser-with-params"));
    const search = screen.getByTestId("search").textContent ?? "";
    expect(new URLSearchParams(search).get("panes")).toBe("editor");
    expect(screen.getByTestId("panes").textContent).toBe("editor,browser");
    expect(search).toContain("selected-block=checkout");
  });

  test("a null value clears a stale param instead of writing it", () => {
    render(
      <MemoryRouter
        initialEntries={["/studio?panes=editor&selected-block=stale"]}
      >
        <StudioPaneDefaultsProvider hasBlocks={true}>
          <OpenWithParamsProbe />
        </StudioPaneDefaultsProvider>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("open-clearing-params"));
    expect(screen.getByTestId("search").textContent).not.toContain(
      "selected-block",
    );
  });

  test("openPane carries selectedBlockLabel even when the pane is already open", () => {
    render(
      <MemoryRouter initialEntries={["/studio?panes=editor,ember"]}>
        <StudioPaneDefaultsProvider hasBlocks={true}>
          <OpenWithParamsProbe />
        </StudioPaneDefaultsProvider>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("open-with-params"));
    expect(screen.getByTestId("search").textContent).toContain(
      "selected-block=checkout",
    );
  });
});
