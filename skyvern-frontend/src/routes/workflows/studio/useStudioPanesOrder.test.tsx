// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { beforeEach, describe, expect, test } from "vitest";

import { useStudioShellStore } from "@/store/StudioShellStore";

import { type StudioPaneId } from "./panes";
import { useStudioPanes } from "./useStudioPanes";

beforeEach(() => {
  localStorage.clear();
  useStudioShellStore.getState().reset();
});

function OrderProbe({ order }: { order: StudioPaneId[] }) {
  const { panes, setPanesOrder, setOpenPanes } = useStudioPanes();
  return (
    <div>
      <output data-testid="panes">{panes.join(",")}</output>
      <button onClick={() => setPanesOrder(order)}>set-order</button>
      <button onClick={() => setOpenPanes(order)}>set-open</button>
    </div>
  );
}

function renderWithPanes(search: string, order: StudioPaneId[]) {
  return render(
    <MemoryRouter initialEntries={[`/studio${search}`]}>
      <OrderProbe order={order} />
    </MemoryRouter>,
  );
}

function CopilotMemoryProbe() {
  const { panes, togglePane, openPane } = useStudioPanes();
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
      <button onClick={() => togglePane("copilot")}>toggle-copilot</button>
      <button onClick={() => togglePane("browser")}>toggle-browser</button>
      <button onClick={() => togglePane("editor")}>toggle-editor</button>
      <button
        onClick={() =>
          openPane("copilot", {
            state: { copilotMessage: "Fix this run" },
          })
        }
      >
        open-copilot-with-state
      </button>
      <button
        onClick={() =>
          navigate(
            "/studio?via=blank&panes=copilot,editor,browser&wr=wr_1#proof",
          )
        }
      >
        inspect-run
      </button>
      <button
        onClick={() =>
          navigate("/studio?via=blank&panes=copilot,editor,browser#proof")
        }
      >
        return-to-studio
      </button>
    </div>
  );
}

function renderCopilotMemory(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <CopilotMemoryProbe />
    </MemoryRouter>,
  );
}

describe("useStudioPanes Copilot context memory", () => {
  test("restores independent Studio and past-run selections without changing the URL", () => {
    const studioAddress =
      "/studio?via=blank&panes=copilot,editor,browser#proof";
    const runAddress =
      "/studio?via=blank&panes=copilot,editor,browser&wr=wr_1#proof";
    renderCopilotMemory(studioAddress);

    expect(screen.getByTestId("panes").textContent).toBe(
      "copilot,editor,browser",
    );
    fireEvent.click(screen.getByText("toggle-copilot"));
    expect(screen.getByTestId("panes").textContent).toBe("editor,browser");
    expect(screen.getByTestId("address").textContent).toBe(studioAddress);

    fireEvent.click(screen.getByText("inspect-run"));
    expect(screen.getByTestId("panes").textContent).toBe(
      "copilot,editor,browser",
    );
    expect(screen.getByTestId("address").textContent).toBe(runAddress);

    // Record an explicit open choice for the run context.
    fireEvent.click(screen.getByText("toggle-copilot"));
    expect(screen.getByTestId("address").textContent).toBe(runAddress);
    fireEvent.click(screen.getByText("toggle-copilot"));
    expect(screen.getByTestId("panes").textContent).toBe(
      "copilot,editor,browser",
    );
    expect(screen.getByTestId("address").textContent).toBe(runAddress);

    fireEvent.click(screen.getByText("return-to-studio"));
    expect(screen.getByTestId("panes").textContent).toBe("editor,browser");
    expect(screen.getByTestId("address").textContent).toBe(studioAddress);

    fireEvent.click(screen.getByText("inspect-run"));
    expect(screen.getByTestId("panes").textContent).toBe(
      "copilot,editor,browser",
    );
    expect(screen.getByTestId("address").textContent).toBe(runAddress);
  });

  test("keeps route-state handoffs while preserving pathname, search, and hash", () => {
    const address = "/studio?via=blank&panes=editor,browser#proof";
    renderCopilotMemory(address);

    fireEvent.click(screen.getByText("open-copilot-with-state"));

    expect(screen.getByTestId("panes").textContent).toBe(
      "editor,browser,copilot",
    );
    expect(screen.getByTestId("address").textContent).toBe(address);
    expect(screen.getByTestId("route-state").textContent).toBe(
      '{"copilotMessage":"Fix this run"}',
    );

    fireEvent.click(screen.getByText("toggle-browser"));
    expect(screen.getByTestId("panes").textContent).toBe("editor,copilot");
    expect(screen.getByTestId("address").textContent).toBe(
      "/studio?via=blank&panes=editor#proof",
    );
    expect(screen.getByTestId("route-state").textContent).toBe(
      '{"copilotMessage":"Fix this run"}',
    );
  });

  test("does not leak a runtime Copilot choice through a later URL pane write", () => {
    renderCopilotMemory("/studio?via=blank&panes=copilot,editor,browser#proof");

    fireEvent.click(screen.getByText("toggle-copilot"));
    fireEvent.click(screen.getByText("toggle-browser"));

    expect(screen.getByTestId("panes").textContent).toBe("editor");
    expect(screen.getByTestId("address").textContent).toBe(
      "/studio?via=blank&panes=copilot,editor#proof",
    );
  });

  test("keeps URL-owned Copilot order through a non-Copilot pane write", () => {
    renderCopilotMemory("/studio?panes=editor,copilot,browser#proof");

    fireEvent.click(screen.getByText("toggle-editor"));

    expect(screen.getByTestId("panes").textContent).toBe("copilot,browser");
    expect(screen.getByTestId("address").textContent).toBe(
      "/studio?panes=copilot,browser#proof",
    );
  });

  test("restores Copilot at its remembered position after reopening", () => {
    const address = "/studio?via=blank&panes=editor,copilot,browser#proof";
    renderCopilotMemory(address);

    fireEvent.click(screen.getByText("toggle-copilot"));
    fireEvent.click(screen.getByText("toggle-copilot"));

    expect(screen.getByTestId("panes").textContent).toBe(
      "editor,copilot,browser",
    );
    expect(screen.getByTestId("address").textContent).toBe(address);
  });
});

describe("useStudioPanes setOpenPanes", () => {
  test("replaces the open set outright (layout override)", () => {
    renderWithPanes("?panes=copilot,browser,overview", ["editor"]);
    fireEvent.click(screen.getByText("set-open"));
    expect(screen.getByTestId("panes").textContent).toBe("editor");
  });
});

describe("useStudioPanes setPanesOrder", () => {
  test("commits a reordered list to the URL", () => {
    renderWithPanes("?panes=copilot,editor,browser", [
      "editor",
      "browser",
      "copilot",
    ]);

    fireEvent.click(screen.getByText("set-order"));

    expect(screen.getByTestId("panes").textContent).toBe(
      "editor,browser,copilot",
    );
  });

  test("keeps the open set from the URL: closed panes in the order are dropped, missing ones appended", () => {
    // "overview" is not open, so it must not open; "browser" is open but
    // absent from the requested order, so it keeps a slot at the end.
    renderWithPanes("?panes=copilot,editor,browser", [
      "overview",
      "editor",
      "copilot",
    ]);

    fireEvent.click(screen.getByText("set-order"));

    expect(screen.getByTestId("panes").textContent).toBe(
      "editor,copilot,browser",
    );
  });

  test("ignores duplicate entries in the requested order", () => {
    renderWithPanes("?panes=copilot,browser", [
      "browser",
      "browser",
      "copilot",
    ]);

    fireEvent.click(screen.getByText("set-order"));

    expect(screen.getByTestId("panes").textContent).toBe("browser,copilot");
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
        <Route path="/runs/:runId/*" element={<RunRouteProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("useStudioPanes under the short run URL", () => {
  test("/runs/{wr} opens the run layout from the path, not the edit default", () => {
    renderAtRunRoute("/runs/wr_1");
    expect(screen.getByTestId("panes").textContent).toBe("browser,overview");
  });

  test("an explicit ?panes= still wins under the short run URL", () => {
    renderAtRunRoute("/runs/wr_1?panes=copilot");
    expect(screen.getByTestId("panes").textContent).toBe("copilot");
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
          openPane("copilot", {
            selectedBlockLabel: "checkout",
          })
        }
      >
        open-with-params
      </button>
      <button
        onClick={() =>
          openPane("copilot", {
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
        <OpenWithParamsProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("open-with-params"));
    // Copilot's open state is runtime-only by design, so the pane change shows
    // up in the pane list while the extra param lands in the one navigation.
    expect(screen.getByTestId("search").textContent).toContain(
      "selected-block=checkout",
    );
    expect(screen.getByTestId("panes").textContent).toContain("copilot");
  });

  test("a pane write that changes the URL pane list carries them too", () => {
    render(
      <MemoryRouter initialEntries={["/studio?panes=editor"]}>
        <OpenWithParamsProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("open-browser-with-params"));
    const search = screen.getByTestId("search").textContent ?? "";
    expect(search).toContain("panes=editor,browser");
    expect(search).toContain("selected-block=checkout");
  });

  test("a null value clears a stale param instead of writing it", () => {
    render(
      <MemoryRouter
        initialEntries={["/studio?panes=editor&selected-block=stale"]}
      >
        <OpenWithParamsProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("open-clearing-params"));
    expect(screen.getByTestId("search").textContent).not.toContain(
      "selected-block",
    );
  });

  test("openPane carries selectedBlockLabel even when the pane is already open", () => {
    render(
      <MemoryRouter initialEntries={["/studio?panes=editor,copilot"]}>
        <OpenWithParamsProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("open-with-params"));
    expect(screen.getByTestId("search").textContent).toContain(
      "selected-block=checkout",
    );
  });
});
