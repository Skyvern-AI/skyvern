// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, test } from "vitest";

import { type StudioPaneId } from "./panes";
import { useStudioPanes } from "./useStudioPanes";

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
