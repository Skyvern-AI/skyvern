// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

vi.mock("@/components/SummarizeOutput", () => ({
  SummarizeOutput: () => null,
}));
vi.mock("./OverviewCodeBlock", () => ({
  OverviewCodeBlock: ({ value }: { value: string }) => <pre>{value}</pre>,
}));

import { RunOutputsSection } from "./RunOutputsSection";

const baseProps = {
  workflowRunId: "wr_1",
  extractedInformation: null,
  files: [],
  summary: null,
  onSummary: () => {},
};

afterEach(cleanup);

describe("RunOutputsSection task 2.0 and webhook surfaces", () => {
  test("renders the webhook failure reason", () => {
    render(
      <RunOutputsSection
        {...baseProps}
        webhookFailureReason="Webhook endpoint returned 500"
      />,
    );
    expect(screen.queryByText("Webhook failure reason")).not.toBeNull();
    expect(screen.queryByText("Webhook endpoint returned 500")).not.toBeNull();
  });

  test("renders the task 2.0 output", () => {
    render(
      <RunOutputsSection {...baseProps} observerOutput={{ answer: 42 }} />,
    );
    expect(screen.queryByText("Task 2.0 output")).not.toBeNull();
    expect(screen.queryByText(/"answer": 42/)).not.toBeNull();
  });

  test("renders nothing without any output signal", () => {
    const { container } = render(<RunOutputsSection {...baseProps} />);
    expect(container.firstChild).toBeNull();
  });
});
