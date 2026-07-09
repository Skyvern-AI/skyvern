// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { StudioShellContext } from "./StudioShellContext";
import { StudioShellPanelPortal } from "./StudioShellPanelPortal";

function renderPortal(open: boolean, onDismiss = vi.fn()) {
  const target = document.createElement("div");
  target.setAttribute("data-testid", "stage-target");
  document.body.appendChild(target);
  const utils = render(
    <StudioShellContext.Provider
      value={{
        copilotPortalEl: null,
        panelPortalEl: target,
        setEditorStreamSlot: () => {},
        setBrowserStreamSlot: () => {},
        setRunStreamSlot: () => {},
      }}
    >
      {/* The panel's OWNER subtree can be display:none (a closed Editor pane);
          the portal must land the content in the shell target regardless. */}
      <div style={{ display: "none" }}>
        <StudioShellPanelPortal open={open} onDismiss={onDismiss}>
          <div data-testid="panel-content">panel</div>
        </StudioShellPanelPortal>
      </div>
    </StudioShellContext.Provider>,
  );
  return { target, onDismiss, ...utils };
}

afterEach(() => {
  cleanup();
  document
    .querySelectorAll('[data-testid="stage-target"]')
    .forEach((el) => el.remove());
});

describe("StudioShellPanelPortal", () => {
  test("renders the panel into the shell target even from a hidden subtree", () => {
    const { target } = renderPortal(true);
    expect(
      target.querySelector('[data-testid="panel-content"]'),
    ).not.toBeNull();
  });

  test("renders nothing while closed", () => {
    const { target } = renderPortal(false);
    expect(target.querySelector('[data-testid="panel-content"]')).toBeNull();
    expect(screen.queryByTestId("panel-content")).toBeNull();
  });

  test("backdrop click and Escape both dismiss", () => {
    const { target, onDismiss } = renderPortal(true);
    fireEvent.click(target.firstElementChild as Element);
    expect(onDismiss).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onDismiss).toHaveBeenCalledTimes(2);
  });
});
