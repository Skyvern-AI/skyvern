import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

import { RunParametersDialog } from "./RunParametersDialog";

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));

const RUN_ID = "wr_sched_e90d5502d63a672a2971";
const WPID = "wpid_evidence";

// Only the shared by-run-id entry is seeded, and no HTTP client is provided, so
// the dialog can only render these inputs by reading that one cache entry.
test("run inputs render from the shared by-run-id cache entry", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  client.setQueryData(["workflowRun", RUN_ID], {
    workflow_run_id: RUN_ID,
    status: "completed",
    parameters: {
      invoice_number: "INV-4471",
      portal_url: "https://billing.example.com/invoices",
      max_retries: 3,
      notify: true,
    },
    workflow: { workflow_permanent_id: WPID, deleted_at: null },
  });
  client.setQueryData(["workflow", WPID], {
    workflow_permanent_id: WPID,
    workflow_definition: {
      parameters: [
        {
          key: "invoice_number",
          parameter_type: "workflow",
          description: "Invoice to look up in the billing portal",
        },
        {
          key: "portal_url",
          parameter_type: "workflow",
          description: "Billing portal entry point",
        },
        { key: "max_retries", parameter_type: "workflow", description: null },
        { key: "notify", parameter_type: "workflow", description: null },
      ],
    },
  });

  const { baseElement } = render(
    <QueryClientProvider client={client}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={[`/agents/${WPID}`]}>
          <Routes>
            <Route
              path="/agents/:workflowPermanentId"
              element={
                <RunParametersDialog
                  open
                  onOpenChange={() => {}}
                  workflowPermanentId={WPID}
                  workflowRunId={RUN_ID}
                />
              }
            />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );

  await waitFor(() =>
    expect(baseElement.textContent).toContain("invoice_number"),
  );
  expect(baseElement.textContent).toContain("Run Inputs");
  expect(baseElement.textContent).toContain("portal_url");
  expect(baseElement.textContent).toContain("max_retries");
});
