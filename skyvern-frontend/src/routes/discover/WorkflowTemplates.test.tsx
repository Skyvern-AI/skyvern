// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkflowTemplates } from "./WorkflowTemplates";

const templates = [
  {
    workflow_permanent_id: "wpid_a",
    title: "State Agency - Annual Report Filing",
  },
  { workflow_permanent_id: "wpid_b", title: "Board - License Lookup" },
  {
    workflow_permanent_id: "wpid_462707285715519276",
    title: "Invoice Downloading",
  },
  { workflow_permanent_id: "wpid_c", title: "County - Business Registration" },
];

vi.mock("../workflows/hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: templates, isLoading: false }),
}));
vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => false,
}));
vi.mock("@/util/homeTelemetry", () => ({
  HomeTelemetry: { templateClicked: vi.fn() },
}));

function cardTitles() {
  return screen
    .getAllByRole("link")
    .map((link) => link.querySelector("[title]")?.getAttribute("title"));
}

describe("WorkflowTemplates", () => {
  afterEach(cleanup);

  it("leads with the most used template and filters by category", () => {
    render(
      <MemoryRouter>
        <WorkflowTemplates />
      </MemoryRouter>,
    );

    expect(cardTitles()[0]).toBe("Invoice Downloading");
    expect(screen.getAllByText("Most used")).toHaveLength(1);
    expect(screen.getAllByRole("link")[0]?.textContent).toContain("Most used");

    fireEvent.click(
      screen.getByRole("button", { name: /Government filings 2/ }),
    );

    expect(cardTitles()).toEqual([
      "State Agency - Annual Report Filing",
      "County - Business Registration",
    ]);
  });
});
