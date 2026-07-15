// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

vi.mock("@/components/SummarizeOutput", () => ({
  SummarizeOutput: ({
    contextKey,
    outputJson,
  }: {
    contextKey: string;
    outputJson: string;
  }) => (
    <div
      data-testid="summarize"
      data-context-key={contextKey}
      data-output-json={outputJson}
    />
  ),
}));
vi.mock("./OverviewCodeBlock", () => ({
  OverviewCodeBlock: ({ value }: { value: string }) => <pre>{value}</pre>,
}));

import { RunOutputsSection } from "./RunOutputsSection";

const baseProps = {
  workflowRunId: "wr_1",
  outputs: null,
  extractedInformation: null,
  files: [],
  errors: [],
  summary: null,
  onSummary: () => {},
};

afterEach(cleanup);

describe("RunOutputsSection agent run outputs", () => {
  const outputs = {
    extracted_information: { answer: 42 },
    additional_output: "full-run-only",
  };

  test("renders extracted information, then agent run outputs, then files", () => {
    render(
      <RunOutputsSection
        {...baseProps}
        outputs={outputs}
        extractedInformation={{ answer: 42 }}
        files={[{ url: "https://x.test/y.pdf", filename: "y.pdf" }]}
      />,
    );
    const labels = screen
      .getAllByText(
        /^(Extracted information|Agent run outputs|Downloaded files)$/,
      )
      .map((el) => el.textContent);
    expect(labels).toEqual([
      "Extracted information",
      "Agent run outputs",
      "Downloaded files",
    ]);
    // The full outputs object carries a field that is absent from extracted info.
    expect(screen.getByText(/full-run-only/)).not.toBeNull();
  });

  test("binds the sole summarizer to the compact full outputs", () => {
    render(
      <RunOutputsSection
        {...baseProps}
        outputs={outputs}
        extractedInformation={{ answer: 42 }}
      />,
    );
    expect(screen.getAllByTestId("summarize")).toHaveLength(1);
    const summarizer = screen.getByTestId("summarize");
    expect(summarizer.getAttribute("data-context-key")).toBe("run:wr_1");
    expect(summarizer.getAttribute("data-output-json")).toBe(
      JSON.stringify(outputs),
    );
  });

  test("shows the generated summary under agent run outputs", () => {
    render(
      <RunOutputsSection
        {...baseProps}
        outputs={outputs}
        extractedInformation={{ answer: 42 }}
        summary="A short summary."
      />,
    );
    expect(screen.getByText("A short summary.")).not.toBeNull();
  });

  test("renders no agent run outputs section when outputs is null", () => {
    render(
      <RunOutputsSection
        {...baseProps}
        files={[{ url: "https://x.test/y.pdf", filename: "y.pdf" }]}
      />,
    );
    expect(screen.queryByText("Agent run outputs")).toBeNull();
    expect(screen.queryByTestId("summarize")).toBeNull();
  });
});

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
