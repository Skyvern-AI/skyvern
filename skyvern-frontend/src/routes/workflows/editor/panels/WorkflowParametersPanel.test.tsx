import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { WorkflowParametersPanel } from "./WorkflowParametersPanel";

vi.mock("@xyflow/react", () => ({
  useNodes: () => [],
  useReactFlow: () => ({ setNodes: vi.fn() }),
}));
vi.mock("./WorkflowParameterEditPanel", () => ({
  WorkflowParameterEditPanel: () => <div>Input form</div>,
}));

function stubViewport(phone: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: phone && query === "(max-width: 719px)",
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

function formPosition(): HTMLElement {
  const form = screen.getByText("Input form").closest<HTMLElement>(".absolute");
  if (!form) throw new Error("input form container not found");
  return form;
}

describe("WorkflowParametersPanel input form", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  test("opens over the panel at phone width, where there is no room beside it", () => {
    stubViewport(true);
    render(<WorkflowParametersPanel />);
    fireEvent.click(screen.getByRole("button", { name: /Add Input/ }));

    expect(formPosition().style.left).toBe("0px");
    expect(formPosition().style.right).toBe("0px");
  });

  test("opens beside the panel on desktop", () => {
    stubViewport(false);
    render(<WorkflowParametersPanel />);
    fireEvent.click(screen.getByRole("button", { name: /Add Input/ }));

    expect(formPosition().style.left).toBe("-336px");
  });
});
