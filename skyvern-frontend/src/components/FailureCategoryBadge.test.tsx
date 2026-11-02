// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { FailureCategory } from "@/api/types";
import { FailureCategoryBadge } from "@/components/FailureCategoryBadge";

const primary: FailureCategory = {
  category: "WEBSITE_ERROR",
  confidence_float: 0.9,
  reasoning: "The site returned an error page",
};

function diagnostic(overrides: Partial<FailureCategory>): FailureCategory {
  return {
    category: "ANTI_BOT_DETECTION",
    confidence_float: 0.8,
    reasoning:
      "datadome enforcement was recorded during the failed attempt; it is not known to have caused it",
    reason_code: "har_anti_bot_diagnostic",
    anti_bot: { contribution: "unknown" },
    ...overrides,
  };
}

describe("FailureCategoryBadge", () => {
  afterEach(cleanup);

  it("keeps the primary category and shows the HAR diagnostic beside it", () => {
    render(
      <FailureCategoryBadge failureCategory={[primary, diagnostic({})]} />,
    );

    expect(screen.getAllByText(/./).map((node) => node.textContent)).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/website/i),
        "Bot protection seen during the run",
      ]),
    );
  });

  it("names the matched configured error code", () => {
    render(
      <FailureCategoryBadge
        failureCategory={[primary, diagnostic({ error_code: "ACCESS_DENIED" })]}
      />,
    );
    expect(screen.getByText("ACCESS_DENIED")).toBeTruthy();
  });

  it("shows only the primary when no diagnostic was recorded", () => {
    render(<FailureCategoryBadge failureCategory={[primary]} />);

    expect(screen.queryByText(/bot protection/i)).toBeNull();
  });

  it("shows a diagnostic-only list as the diagnostic, not as a bot-protection block", () => {
    render(
      <FailureCategoryBadge
        failureCategory={[diagnostic({ error_code: "ACCESS_DENIED" })]}
      />,
    );

    expect(screen.getByText("ACCESS_DENIED")).toBeTruthy();
    expect(screen.queryByText(/blocked by bot protection/i)).toBeNull();
  });

  it("says the diagnostic's contribution is unknown even for a configured code", async () => {
    render(
      <FailureCategoryBadge
        failureCategory={[
          primary,
          diagnostic({
            error_code: "ACCESS_DENIED",
            reasoning: "ACCESS_DENIED: The portal refused access",
          }),
        ]}
      />,
    );

    fireEvent.focus(screen.getByText("ACCESS_DENIED"));

    const [tooltip] = await screen.findAllByRole("tooltip");
    expect(tooltip?.textContent).toMatch(
      /ACCESS_DENIED: The portal refused access/,
    );
    expect(tooltip?.textContent).toMatch(/contribution.*unknown/i);
  });
});
