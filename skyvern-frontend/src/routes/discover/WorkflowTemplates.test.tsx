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
  ...Array.from({ length: 11 }, (_, index) => ({
    workflow_permanent_id: `wpid_template_${index}`,
    title: `Template ${index + 1}`,
  })),
];

vi.mock("../workflows/hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: templates, isLoading: false }),
}));
vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => false,
}));
const mutate = vi.hoisted(() => vi.fn());
const mutation = vi.hoisted(() => ({ isPending: false }));
vi.mock("../workflows/hooks/useCreateWorkflowMutation", () => ({
  useCreateWorkflowMutation: () => ({ mutate, isPending: mutation.isPending }),
}));
vi.mock("../workflows/editor/workflowEditorUtils", () => ({
  convert: (workflow: { title: string }) => ({ title: workflow.title }),
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
  afterEach(() => {
    cleanup();
    mutate.mockClear();
    mutation.isPending = false;
  });

  it("copies the template on a plain click but not a modified click", () => {
    render(
      <MemoryRouter>
        <WorkflowTemplates folderId="fld_1" />
      </MemoryRouter>,
    );
    const card = screen.getAllByRole("link")[0]!;

    fireEvent.click(card, { metaKey: true });
    expect(mutate).not.toHaveBeenCalled();

    fireEvent.click(card);
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Invoice Downloading (copy)",
        folder_id: "fld_1",
        _via: "template",
      }),
    );
  });

  it("ignores clicks while a copy is already being created", () => {
    mutation.isPending = true;
    render(
      <MemoryRouter>
        <WorkflowTemplates />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getAllByRole("link")[0]!);
    expect(mutate).not.toHaveBeenCalled();
  });

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

  it("expands all templates and collapses back to six", () => {
    render(
      <MemoryRouter>
        <WorkflowTemplates />
      </MemoryRouter>,
    );

    const cards = screen.getAllByRole("link");
    const isHiddenAtMd = (card: Element) =>
      card.classList.contains("md:hidden");
    const expandButton = screen.getByRole("button", { name: "See all 15" });

    expect(cards).toHaveLength(15);
    expect(expandButton.getAttribute("aria-expanded")).toBe("false");
    expect(cards.slice(0, 6).every((card) => !isHiddenAtMd(card))).toBe(true);
    expect(cards.slice(6).every(isHiddenAtMd)).toBe(true);

    fireEvent.click(expandButton);

    const collapseButton = screen.getByRole("button", { name: "Show fewer" });
    expect(collapseButton.getAttribute("aria-expanded")).toBe("true");
    expect(cards.every((card) => !isHiddenAtMd(card))).toBe(true);

    fireEvent.click(collapseButton);

    expect(
      screen
        .getByRole("button", { name: "See all 15" })
        .getAttribute("aria-expanded"),
    ).toBe("false");
    expect(cards.slice(0, 6).every((card) => !isHiddenAtMd(card))).toBe(true);
    expect(cards.slice(6).every(isHiddenAtMd)).toBe(true);
  });
});
