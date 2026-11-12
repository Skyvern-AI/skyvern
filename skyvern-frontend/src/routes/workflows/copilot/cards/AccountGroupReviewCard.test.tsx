// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EMPTY_NARRATIVE } from "../narrativeState";
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

  it("approves a single run with exactly the accounts it shows, with nothing to uncheck", () => {
    const onApprove = vi.fn();
    render(
      <AccountGroupReviewCard
        review={{
          ...review,
          credential_parameter_key: "",
          single_run: true,
          rows: [
            { ...review.rows[2]!, parameter_key: "login" },
            { ...review.rows[1]!, parameter_key: "portal" },
          ],
        }}
        disabled={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onApprove={onApprove}
        onDecline={() => {}}
      />,
    );
    expect(screen.getByText("login").nextSibling?.textContent).toBe("Ops");
    expect(screen.getByText("portal").nextSibling?.textContent).toBe("Payroll");
    expect(screen.queryByRole("checkbox")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Run once" }));

    expect(onApprove).toHaveBeenCalledWith(["cred_c", "cred_b"]);
  });

  it("says a draft run saves a new version first, whether it was tested, and opens its changes", () => {
    const onApprove = vi.fn();
    render(
      <AccountGroupReviewCard
        review={{
          ...review,
          credential_parameter_key: "",
          single_run: true,
          saves_draft: true,
          proposal_owner_turn_id: "turn-draft",
          proposal_revision: 2,
          tested_workflow_run_id: null,
          rows: [],
        }}
        draftTurn={{
          ...EMPTY_NARRATIVE,
          turnId: "turn-draft",
          turnIndex: 0,
          draft: {
            blockCount: 1,
            blockLabels: ["download_statement"],
            summary: null,
          },
          proposalDisposition: "review_untested",
        }}
        disabled={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onApprove={onApprove}
        onDecline={() => {}}
      />,
    );
    expect(
      screen.getByText(
        "Saves this draft as a new version, then starts the run",
      ),
    ).toBeTruthy();
    expect(screen.getByText(/saved as a new version/)).toBeTruthy();
    expect(screen.queryByText(/version 4/)).toBeNull();
    expect(screen.getByText("This draft has not been tested.")).toBeTruthy();
    expect(screen.queryByText(/download_statement/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show changes" }));
    expect(screen.getByText(/download_statement/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save and run" }));

    expect(onApprove).toHaveBeenCalledWith([]);
  });

  it("calls a tested draft tested when its test run id is not recorded", () => {
    render(
      <AccountGroupReviewCard
        review={{
          ...review,
          credential_parameter_key: "",
          single_run: true,
          saves_draft: true,
          draft_tested: true,
          tested_workflow_run_id: null,
          rows: [],
        }}
        disabled={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onApprove={() => {}}
        onDecline={() => {}}
      />,
    );
    expect(screen.getByText("Tested in a test run")).toBeTruthy();
    expect(screen.queryByText("This draft has not been tested.")).toBeNull();
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
