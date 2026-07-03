// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test } from "vitest";

import { useStudioFirstRunStore } from "@/store/StudioFirstRunStore";

import { StudioCoachMark } from "./StudioCoachMark";

function renderAt(path = "/workflows/wpid_1/studio") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <StudioCoachMark />
    </MemoryRouter>,
  );
}

afterEach(cleanup);
beforeEach(() => {
  localStorage.clear();
  useStudioFirstRunStore.setState({
    coachMarkSeen: false,
    narrowNudgeSeen: false,
  });
});

describe("StudioCoachMark", () => {
  test("shows on a first plain studio visit", () => {
    renderAt();
    expect(screen.getByRole("note", { name: "Studio panes tip" })).toBeTruthy();
  });

  test("dismisses for good via Got it", () => {
    renderAt();
    fireEvent.click(screen.getByRole("button", { name: "Got it" }));
    expect(screen.queryByRole("note")).toBeNull();
    expect(useStudioFirstRunStore.getState().coachMarkSeen).toBe(true);
  });

  test("never shows again once seen", () => {
    useStudioFirstRunStore.setState({ coachMarkSeen: true });
    renderAt();
    expect(screen.queryByRole("note")).toBeNull();
  });

  test("hides live when a pane interaction marks it seen", () => {
    renderAt();
    expect(screen.getByRole("note")).toBeTruthy();
    act(() => {
      useStudioFirstRunStore.getState().markCoachMarkSeen();
    });
    expect(screen.queryByRole("note")).toBeNull();
  });

  test("stays out of the way on deep-linked visits", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1");
    expect(screen.queryByRole("note")).toBeNull();
  });
});
