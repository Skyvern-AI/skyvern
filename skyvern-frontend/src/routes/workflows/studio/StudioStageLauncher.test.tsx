// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { StudioStageLauncher } from "./StudioStageLauncher";

const { runSignalsMock } = vi.hoisted(() => ({
  runSignalsMock: vi.fn(),
}));

vi.mock("./useStudioRunSignals", () => ({
  useStudioRunSignals: () => runSignalsMock(),
}));

const initialBrowserState = useStudioBrowserStore.getState();

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="search">{location.search}</output>;
}

function renderAt(path = "/workflows/wpid_1/studio?panes=") {
  return render(
    <TooltipProvider delayDuration={0}>
      <MemoryRouter initialEntries={[path]}>
        <StudioStageLauncher />
        <LocationProbe />
      </MemoryRouter>
    </TooltipProvider>,
  );
}

function currentPanes(): string | null {
  const search = screen.getByTestId("search").textContent ?? "";
  return new URLSearchParams(search).get("panes");
}

afterEach(cleanup);
beforeEach(() => {
  useStudioBrowserStore.setState(initialBrowserState, true);
  runSignalsMock.mockReturnValue({
    hasRun: false,
    runStatus: undefined,
    knownHasRuns: false,
  });
});

describe("StudioStageLauncher", () => {
  test("offers every pane as a labeled button", () => {
    renderAt();
    for (const label of ["Copilot", "Editor", "Browser", "Overview"]) {
      expect(
        screen.getByRole("button", { name: new RegExp(`^${label}`) }),
      ).toBeTruthy();
    }
  });

  test("keeps the Overview launcher gated until a run exists, with the reason readable", () => {
    renderAt();
    const timeline = screen.getByRole("button", { name: /no runs yet/ });
    expect((timeline as HTMLButtonElement).disabled).toBe(true);
  });

  test("enables the Overview launcher once a run exists", () => {
    runSignalsMock.mockReturnValue({
      hasRun: true,
      runStatus: undefined,
      knownHasRuns: true,
    });
    renderAt();
    const timeline = screen.getByRole("button", { name: "Overview" });
    expect((timeline as HTMLButtonElement).disabled).toBe(false);
  });

  test("opens the clicked pane", () => {
    renderAt();
    fireEvent.click(screen.getByRole("button", { name: "Editor" }));
    expect(currentPanes()).toBe("editor");
  });

  test("clears unseen browser activity when opening the Browser pane", () => {
    useStudioBrowserStore.getState().markActivity();
    renderAt();
    fireEvent.click(screen.getByRole("button", { name: "Browser" }));
    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
    expect(currentPanes()).toBe("browser");
  });
});
