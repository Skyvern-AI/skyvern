import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import { CollapseContext, useCollapseContext } from "./CollapseContext";

afterEach(cleanup);

function Probe() {
  const { open } = useCollapseContext();
  return <div data-testid="probe">{open ? "open" : "closed"}</div>;
}

describe("CollapseContext", () => {
  test("defaults to open=true when no provider", () => {
    render(<Probe />);
    expect(screen.getByTestId("probe").textContent).toBe("open");
  });

  test("provider value flows to consumers", () => {
    render(
      <CollapseContext.Provider value={{ open: false }}>
        <Probe />
      </CollapseContext.Provider>,
    );
    expect(screen.getByTestId("probe").textContent).toBe("closed");
  });
});
