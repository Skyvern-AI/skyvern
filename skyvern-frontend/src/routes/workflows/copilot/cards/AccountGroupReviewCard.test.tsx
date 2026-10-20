// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AccountGroupReview } from "../workflowCopilotTypes";
import {
  AccountGroupCancelCard,
  AccountGroupReviewCard,
} from "./AccountGroupReviewCard";

afterEach(cleanup);

const review: AccountGroupReview = {
  workflow_permanent_id: "wpid_1",
  workflow_id: "wf_1",
  version: 3,
  workflow_title: "Download statement",
  credential_parameter_key: "login",
  action_summary: "Download the latest statement",
  common_inputs: { month: "May" },
  rows: [
    {
      credential_id: "cred_a",
      label: "Billing (al***@example.com)",
      prior_outcome: "completed",
      preselected: false,
    },
    {
      credential_id: "cred_b",
      label: "Payroll",
      prior_outcome: "failed",
      preselected: true,
    },
    {
      credential_id: "cred_c",
      label: "Ops",
      prior_outcome: null,
      preselected: true,
    },
  ],
  workflow_run_group_id: null,
};

describe("account group review", () => {
  it("approves exactly the accounts left checked, with repeat-risk rows starting unchecked", () => {
    const onApprove = vi.fn();
    render(
      <AccountGroupReviewCard
        review={review}
        disabled={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onApprove={onApprove}
        onDecline={() => {}}
      />,
    );
    expect(screen.getByText(/already completed last time/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText(/Payroll/));

    fireEvent.click(screen.getByRole("button", { name: "Approve 1 run" }));

    expect(onApprove).toHaveBeenCalledWith(["cred_c"]);
  });

  it("lists the runs a cancel would stop and answers stop or keep", () => {
    const onDecide = vi.fn();
    render(
      <AccountGroupCancelCard
        review={{
          workflow_run_group_id: "wrg_1",
          unfinished_rows: [
            {
              credential_id: "cred_b",
              label: "Payroll",
              workflow_run_id: "wr_b",
              outcome: "in_progress",
            },
            {
              credential_id: "cred_c",
              label: "Ops",
              workflow_run_id: "wr_c",
              outcome: "pending",
            },
          ],
        }}
        disabled={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onDecide={onDecide}
      />,
    );
    expect(screen.getByText("Payroll")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Keep running" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop 2 runs" }));

    expect(onDecide.mock.calls).toEqual([[false], [true]]);
  });
});
