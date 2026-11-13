// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EMPTY_NARRATIVE } from "../narrativeState";
import type {
  AccountGroupReview,
  QuestionInteraction,
  RunCostFacts,
} from "../workflowCopilotTypes";
import { AccountGroupReceiptCard } from "./AccountGroupReceiptCard";
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

  it("states the cost facts of a metered run and multiplies the estimate by the checked accounts", () => {
    const cost: RunCostFacts = {
      runs_to_start: 2,
      based_on_run_id: "wr_tested",
      based_on_credits: 42,
      blocks_completed: 2,
      blocks_total: 2,
      estimate_from_one_run: 84,
      balance_credits: 500,
      stop_rule:
        "Each run checks this account's credits before it starts, and its steps check again while it runs.",
    };
    render(
      <AccountGroupReviewCard
        review={{
          ...review,
          cost,
          based_on_inputs: { month: "May", login: "[REDACTED_SECRET]" },
        }}
        disabled={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onApprove={() => {}}
        onDecline={() => {}}
      />,
    );
    const facts = within(screen.getByLabelText("Cost"));
    expect(facts.getByText("Runs to start").nextSibling?.textContent).toBe("2");
    expect(
      facts.getByRole("link", { name: /the latest run/ }).getAttribute("href"),
    ).toBe("/runs/wr_tested");
    expect(facts.getByText("2 of 2")).toBeTruthy();
    expect(facts.getByText("Its inputs")).toBeTruthy();
    expect(facts.getByText("month").nextSibling?.textContent).toBe("May");
    expect(facts.getByText("[REDACTED_SECRET]")).toBeTruthy();
    expect(facts.getByText("84 credits (estimate from one run)")).toBeTruthy();
    expect(facts.getByText("500 credits")).toBeTruthy();
    expect(
      facts.getByText(
        "Each run checks this account's credits before it starts, and its steps check again while it runs.",
      ),
    ).toBeTruthy();

    fireEvent.click(screen.getByLabelText(/Payroll/));

    expect(facts.getByText("Runs to start").nextSibling?.textContent).toBe("1");
    expect(facts.getByText("42 credits (estimate from one run)")).toBeTruthy();

    fireEvent.click(screen.getByLabelText(/Ops/));

    expect(facts.queryByText("Runs to start")).toBeNull();
    expect(facts.queryByText(/estimate from one run/)).toBeNull();
    expect(facts.getByText("42 credits")).toBeTruthy();
  });

  it("names the draft's test run once and shows no run, blocks or estimate when no run qualifies", () => {
    const { rerender } = render(
      <AccountGroupReviewCard
        review={{
          ...review,
          single_run: true,
          saves_draft: true,
          draft_tested: true,
          tested_workflow_run_id: "wr_tested",
          cost: {
            runs_to_start: 1,
            based_on_run_id: "wr_tested",
            based_on_credits: 42,
            blocks_completed: 1,
            blocks_total: 1,
            estimate_from_one_run: 42,
          },
        }}
        disabled={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onApprove={() => {}}
        onDecline={() => {}}
      />,
    );
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(
      within(screen.getByLabelText("Cost")).getByText("the test run"),
    ).toBeTruthy();

    rerender(
      <AccountGroupReviewCard
        review={{
          ...review,
          single_run: true,
          cost: {
            runs_to_start: 1,
            balance_credits: 500,
            stop_rule:
              "Each run checks this account's credits before it starts, and its steps check again while it runs.",
          },
        }}
        disabled={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onApprove={() => {}}
        onDecline={() => {}}
      />,
    );
    const facts = within(screen.getByLabelText("Cost"));
    expect(facts.queryByText("Based on")).toBeNull();
    expect(facts.queryByText("Blocks it completed")).toBeNull();
    expect(facts.queryByText(/estimate from one run/)).toBeNull();
    expect(facts.getByText("Runs to start")).toBeTruthy();
  });

  it("shows no estimate for a partial run and no number at all when credits are not tracked", () => {
    const { rerender } = render(
      <AccountGroupReviewCard
        review={{
          ...review,
          single_run: true,
          cost: {
            runs_to_start: 1,
            based_on_run_id: "wr_partial",
            based_on_credits: 12,
            blocks_completed: 1,
            blocks_total: 3,
            balance_credits: 500,
          },
        }}
        disabled={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onApprove={() => {}}
        onDecline={() => {}}
      />,
    );
    expect(screen.getByText("1 of 3")).toBeTruthy();
    expect(screen.queryByText(/estimate from one run/)).toBeNull();

    const note = "Custom enterprise billing, so credit counters do not apply.";
    rerender(
      <AccountGroupReviewCard
        review={{
          ...review,
          cost: {
            based_on_run_id: "wr_7",
            credits_note: note,
            stop_rule: "Nothing stops these runs except Stop.",
          },
        }}
        disabled={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onApprove={() => {}}
        onDecline={() => {}}
      />,
    );
    const facts = screen.getByLabelText("Cost");
    expect(within(facts).getByText(note)).toBeTruthy();
    expect(within(facts).queryByText("Runs to start")).toBeNull();
    expect(facts.textContent?.match(/\d/)).toBeNull();
  });
});

describe("account group receipt", () => {
  const cost: RunCostFacts = {
    runs_to_start: 1,
    based_on_run_id: "wr_tested",
    based_on_credits: 42,
    blocks_completed: 1,
    blocks_total: 1,
    estimate_from_one_run: 42,
    balance_credits: 500,
  };
  const interaction = (stored: AccountGroupReview): QuestionInteraction =>
    JSON.parse(
      JSON.stringify({
        interaction_id: "interaction",
        turn_id: "turn",
        tool_call_id: "call",
        status: "resolved",
        response: null,
        created_at: "2026-11-12T00:00:00Z",
        resolved_at: "2026-11-12T00:01:00Z",
        parts: [],
        account_group_review: stored,
      }),
    );

  const renderReceipt = (shape: Partial<AccountGroupReview>) => {
    const stored = interaction({
      ...review,
      ...shape,
      cost,
      decision: { approved: true, credential_ids: ["cred_b", "cred_c"] },
    });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AccountGroupReceiptCard
          interaction={stored}
          review={stored.account_group_review!}
          turnLive={false}
        />
      </QueryClientProvider>,
    );
    return within(screen.getByRole("group", { name: "Cost" }));
  };

  it.each([
    [
      "one run",
      { single_run: true, workflow_run_id: "wr_1", run_started: true },
      "1",
      "42 credits (estimate from one run)",
    ],
    [
      "a group",
      { workflow_run_group_id: "wrg_1" },
      "2",
      "84 credits (estimate from one run)",
    ],
  ])(
    "keeps the approved %s's cost facts after a reload",
    (_, shape, count, estimate) => {
      const facts = renderReceipt(shape);
      expect(facts.getByText(estimate)).toBeTruthy();
      expect(facts.getByText("Estimate at review")).toBeTruthy();
      expect(facts.getByText("Runs approved").nextSibling?.textContent).toBe(
        count,
      );
      expect(facts.getByText("1 of 1")).toBeTruthy();
      expect(
        facts.getByText("Balance at review").nextSibling?.textContent,
      ).toBe("500 credits");
    },
  );

  it.each([
    ["one run with no linked run", { single_run: true }],
    [
      "one run that failed to start",
      { single_run: true, workflow_run_id: "wr_1", run_started: false },
    ],
    ["a group with no linked group", {}],
  ])("shows no run count or estimate for %s", (_, shape) => {
    const facts = renderReceipt(shape);
    expect(facts.queryByText("Runs approved")).toBeNull();
    expect(facts.queryByText("Estimate at review")).toBeNull();
    expect(facts.getByText("That run used")).toBeTruthy();
  });
});
