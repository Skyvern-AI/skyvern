// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  CredentialDeleteReview,
  QuestionInteraction,
} from "../workflowCopilotTypes";
import {
  CredentialDeleteReceipt,
  CredentialDeleteReviewCard,
} from "./CredentialDeleteCard";

afterEach(cleanup);

const review: CredentialDeleteReview = {
  rows: [
    { credential_id: "cred_a", name: "Billing", credential_type: "password" },
    {
      credential_id: "cred_b",
      name: "Payments key",
      credential_type: "secret",
    },
  ],
  total_credential_count: 5,
};

describe("credential deletion card", () => {
  it("confirms only the entries left checked and never presents a subset as the whole vault", () => {
    const onConfirm = vi.fn();
    render(
      <CredentialDeleteReviewCard
        review={review}
        disabled={false}
        deleting={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onConfirm={onConfirm}
        onCancel={() => {}}
      />,
    );
    expect(
      screen.getByText(/lists 2 of your 5 saved credentials/),
    ).toBeTruthy();
    expect(
      screen.getByText(/Ask Copilot for another card to delete the rest/),
    ).toBeTruthy();
    expect(screen.getByText(/cannot be undone/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText(/Billing/));

    fireEvent.click(
      screen.getByRole("button", { name: "Delete 1 credential" }),
    );

    expect(onConfirm).toHaveBeenCalledWith(["cred_b"]);
  });

  it("tells apart entries that share a name", () => {
    const onConfirm = vi.fn();
    render(
      <CredentialDeleteReviewCard
        review={{
          rows: [
            {
              credential_id: "cred_x",
              name: "Login",
              credential_type: "password",
            },
            {
              credential_id: "cred_y",
              name: "Login",
              credential_type: "password",
            },
          ],
          total_credential_count: 2,
        }}
        disabled={false}
        deleting={false}
        lockReason={null}
        collapsed={false}
        onCollapsedChange={() => {}}
        onConfirm={onConfirm}
        onCancel={() => {}}
      />,
    );
    fireEvent.click(screen.getByLabelText(/cred_x/));

    fireEvent.click(
      screen.getByRole("button", { name: "Delete 1 credential" }),
    );

    expect(onConfirm).toHaveBeenCalledWith(["cred_y"]);
  });

  it("reports each recorded outcome instead of claiming everything was deleted", () => {
    const interaction: QuestionInteraction = {
      interaction_id: "q1",
      turn_id: "t1",
      tool_call_id: "c1",
      parts: [],
      status: "resolved",
      response: {},
      created_at: "2026-10-05T00:00:00Z",
      resolved_at: "2026-10-05T00:00:05Z",
      credential_delete_review: review,
    };
    render(
      <CredentialDeleteReceipt
        interaction={interaction}
        review={{
          ...review,
          outcomes: [
            { credential_id: "cred_a", outcome: "deleted" },
            { credential_id: "cred_b", outcome: "failed" },
          ],
        }}
      />,
    );

    expect(screen.getByText("Deleted 1 of 2 credentials")).toBeTruthy();
    expect(screen.getByText(/Failed, may still be saved/)).toBeTruthy();
  });
});
