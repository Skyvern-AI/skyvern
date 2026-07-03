// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { Status } from "@/api/types";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { StudioSpine } from "./StudioSpine";

const { runsQueryMock, runWithWorkflowMock } = vi.hoisted(() => ({
  runsQueryMock: vi.fn(),
  runWithWorkflowMock: vi.fn(),
}));

vi.mock("../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => runsQueryMock(),
}));

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => runWithWorkflowMock(),
}));

const initialBrowserState = useStudioBrowserStore.getState();

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="search">{location.search}</output>;
}

function renderAt(path = "/workflows/wpid_abc/studio") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <StudioSpine />
      <LocationProbe />
    </MemoryRouter>,
  );
}

function tab(name: RegExp | string): HTMLButtonElement {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

function currentPanes(): string | null {
  const search = screen.getByTestId("search").textContent ?? "";
  return new URLSearchParams(search).get("panes");
}

afterEach(cleanup);
beforeEach(() => {
  useStudioBrowserStore.setState(initialBrowserState, true);
  runsQueryMock.mockReturnValue({ data: [] });
  runWithWorkflowMock.mockReturnValue({ data: undefined });
});

describe("StudioSpine structure", () => {
  test("renders the four peer tabs with icon + label", () => {
    renderAt();
    for (const label of ["Copilot", "Editor", "Browser", "Timeline"]) {
      expect(tab(new RegExp(`^${label}`))).toBeTruthy();
    }
  });

  test("reflects the default panes (copilot + browser) as expanded", () => {
    renderAt();
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Browser/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Editor/).getAttribute("aria-expanded")).toBe("false");
  });

  test("reflects an explicit ?panes= list", () => {
    renderAt("/workflows/wpid_abc/studio?panes=editor");
    expect(tab(/^Editor/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("false");
    expect(tab(/^Browser/).getAttribute("aria-expanded")).toBe("false");
  });

  test("explicit ?panes= wins over a run deep link", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Completed }] });
    renderAt("/workflows/wpid_abc/studio?wr=run_1&panes=copilot");
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Timeline/).getAttribute("aria-expanded")).toBe("false");
  });

  test("a block-run deep link opens Editor, Browser and Timeline", () => {
    renderAt("/workflows/wpid_abc/studio?wr=run_1&bl=block_1");
    expect(tab(/^Editor/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Browser/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Timeline/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("false");
  });
});

describe("StudioSpine pane toggling", () => {
  test("opening a closed pane appends it in click order", () => {
    renderAt();
    fireEvent.click(tab(/^Editor/));
    expect(currentPanes()).toBe("copilot,browser,editor");
    expect(tab(/^Editor/).getAttribute("aria-expanded")).toBe("true");
  });

  test("closing an open pane splices it out, keeping the rest in order", () => {
    renderAt("/workflows/wpid_abc/studio?panes=editor,copilot,browser");
    fireEvent.click(tab(/^Copilot/));
    expect(currentPanes()).toBe("editor,browser");
  });

  test("closing the last pane leaves an explicit empty list", () => {
    renderAt("/workflows/wpid_abc/studio?panes=editor");
    fireEvent.click(tab(/^Editor/));
    expect(currentPanes()).toBe("");
  });

  test("toggling preserves unrelated params", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Completed }] });
    renderAt("/workflows/wpid_abc/studio?wr=run_1&bl=block_1");
    fireEvent.click(tab(/^Copilot/));
    const search = screen.getByTestId("search").textContent ?? "";
    const params = new URLSearchParams(search);
    expect(params.get("wr")).toBe("run_1");
    expect(params.get("bl")).toBe("block_1");
    expect(params.get("panes")).toBe("editor,browser,timeline,copilot");
  });
});

describe("StudioSpine Timeline gating", () => {
  test("disables the Timeline tab until a run exists", () => {
    renderAt();
    expect(tab(/^Timeline/).disabled).toBe(true);
  });

  test("enables the Timeline tab when the workflow has a prior run", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Running }] });
    renderAt();
    expect(tab(/^Timeline/).disabled).toBe(false);
  });

  test("enables the Timeline tab when the URL points at a run (?wr=)", () => {
    renderAt("/workflows/wpid_abc/studio?wr=run_1&panes=copilot");
    expect(tab(/^Timeline/).disabled).toBe(false);
  });
});

describe("StudioSpine run-status dot", () => {
  test("shows a status-colored dot for a finalized run", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Completed }] });
    renderAt();
    const dot = tab(/^Timeline/).querySelector(
      "span.absolute.right-1",
    ) as HTMLElement | null;
    expect(dot).not.toBeNull();
    expect(dot?.className).toContain("bg-badge-success");
  });

  test("includes the finalized run status in the Timeline tab accessible name", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.TimedOut }] });
    renderAt();
    expect(
      screen.getByRole("button", { name: "Timeline, timed out" }),
    ).toBeTruthy();
  });

  test("omits the dot while the run is still in flight", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Running }] });
    renderAt();
    expect(tab(/^Timeline/).querySelector("span.absolute.right-1")).toBeNull();
  });
});

describe("StudioSpine browser activity", () => {
  test("exposes unseen activity on the Browser tab while its pane is closed", () => {
    useStudioBrowserStore.getState().markActivity();
    renderAt("/workflows/wpid_abc/studio?panes=copilot");
    expect(
      screen.getByRole("button", { name: "Browser, new activity" }),
    ).toBeTruthy();
  });

  test("hides the activity dot while the Browser pane is open", () => {
    useStudioBrowserStore.getState().markActivity();
    renderAt("/workflows/wpid_abc/studio?panes=browser");
    expect(
      screen.queryByRole("button", { name: "Browser, new activity" }),
    ).toBeNull();
  });

  test("clears unseen activity when the Browser pane is opened", () => {
    useStudioBrowserStore.getState().markActivity();
    renderAt("/workflows/wpid_abc/studio?panes=copilot");
    fireEvent.click(tab(/Browser/));
    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
    expect(currentPanes()).toBe("copilot,browser");
  });
});

describe("StudioSpine keyboard navigation", () => {
  test("the rail is a single tab stop (roving tabindex)", () => {
    renderAt();
    expect(
      ["Copilot", "Editor", "Browser"].map(
        (l) => tab(new RegExp(`^${l}`)).tabIndex,
      ),
    ).toEqual([0, -1, -1]);
  });

  test("ArrowDown moves focus and wraps past the disabled Timeline tab", () => {
    renderAt();
    tab(/^Copilot/).focus();
    fireEvent.keyDown(tab(/^Copilot/), { key: "ArrowDown" });
    expect(document.activeElement).toBe(tab(/^Editor/));
    fireEvent.keyDown(tab(/^Editor/), { key: "ArrowDown" });
    expect(document.activeElement).toBe(tab(/^Browser/));
    fireEvent.keyDown(tab(/^Browser/), { key: "ArrowDown" });
    expect(document.activeElement).toBe(tab(/^Copilot/));
  });

  test("ArrowUp wraps to the last enabled tab and Home returns to the first", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Completed }] });
    renderAt();
    tab(/^Copilot/).focus();
    fireEvent.keyDown(tab(/^Copilot/), { key: "ArrowUp" });
    expect(document.activeElement).toBe(tab(/^Timeline/));
    fireEvent.keyDown(tab(/^Timeline/), { key: "Home" });
    expect(document.activeElement).toBe(tab(/^Copilot/));
  });

  test("arrow keys move focus without toggling panes", () => {
    renderAt();
    fireEvent.keyDown(tab(/^Copilot/), { key: "ArrowDown" });
    expect(currentPanes()).toBeNull();
    expect(tab(/^Editor/).getAttribute("aria-expanded")).toBe("false");
  });
});
