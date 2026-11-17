import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { CreateScheduleDialog } from "@/routes/workflows/editor/panels/schedulePanel/CreateScheduleDialog";
import { CreateOrgScheduleDialog } from "./CreateOrgScheduleDialog";

afterEach(cleanup);

const SCROLL_CLASSES = ["overflow-y-auto", "overflow-auto", "overflow-scroll"];

// jsdom applies no stylesheet, so a scroll container is only identifiable by
// the utility classes that would give it one in the browser.
function scrollsItsOwnContent(element: Element): boolean {
  const classes = element.className.split(/\s+/);
  return SCROLL_CLASSES.some((scrollClass) => classes.includes(scrollClass));
}

function renderOrgDialog() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <CreateOrgScheduleDialog open onOpenChange={() => {}} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

function renderWorkflowPanelDialog() {
  render(<CreateScheduleDialog workflowParameters={[]} onSubmit={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
}

describe.each([
  ["schedules page", renderOrgDialog],
  ["workflow editor schedule panel", renderWorkflowPanelDialog],
])("Create Schedule dialog on the %s", (_surface, renderDialog) => {
  // A tall form used to scroll the whole dialog, carrying Cancel and Create
  // Schedule out of view until the user scrolled back down to them.
  it("keeps its actions outside every scrolling region", () => {
    renderDialog();

    const dialog = screen.getByRole("dialog");
    const actions = [
      screen.getByRole("button", { name: "Cancel" }),
      screen.getByRole("button", { name: "Create Schedule" }),
    ];

    for (const action of actions) {
      for (
        let node: Element | null = action;
        node !== null && node !== dialog.parentElement;
        node = node.parentElement
      ) {
        expect(scrollsItsOwnContent(node)).toBe(false);
      }
    }
  });

  it("scrolls the form fields instead", () => {
    renderDialog();

    const body = screen.getByTestId("create-schedule-dialog-body");

    expect(scrollsItsOwnContent(body)).toBe(true);
    expect(body.contains(screen.getByText("Repeat"))).toBe(true);
  });
});
