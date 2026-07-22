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

describe("RunOutputsSection run outputs", () => {
  const outputs = {
    extracted_information: { answer: 42 },
    additional_output: "full-run-only",
  };

  test("renders extracted information, then run outputs, then files", () => {
    render(
      <RunOutputsSection
        {...baseProps}
        outputs={outputs}
        extractedInformation={{ answer: 42 }}
        files={[{ url: "https://x.test/y.pdf", filename: "y.pdf" }]}
      />,
    );
    const labels = screen
      .getAllByText(/^(Extracted information|Run outputs|Downloaded files)$/)
      .map((el) => el.textContent);
    expect(labels).toEqual([
      "Extracted information",
      "Run outputs",
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

  test("shows the generated summary under run outputs", () => {
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

  test("renders no run outputs section when outputs is null", () => {
    render(
      <RunOutputsSection
        {...baseProps}
        files={[{ url: "https://x.test/y.pdf", filename: "y.pdf" }]}
      />,
    );
    expect(screen.queryByText("Run outputs")).toBeNull();
    expect(screen.queryByTestId("summarize")).toBeNull();
  });

  test("hides the run outputs header when outputs holds only extracted_information", () => {
    render(
      <RunOutputsSection
        {...baseProps}
        outputs={{ extracted_information: { answer: 42 } }}
        extractedInformation={{ answer: 42 }}
      />,
    );
    // Extracted information keeps its own dedicated section.
    expect(screen.queryByText("Extracted information")).not.toBeNull();
    // No per-field run outputs remain, so no empty header or Summarize button.
    expect(screen.queryByText("Run outputs")).toBeNull();
    expect(screen.queryByTestId("summarize")).toBeNull();
  });

  test("keeps the run outputs block when only a persisted summary remains", () => {
    render(
      <RunOutputsSection
        {...baseProps}
        outputs={{ extracted_information: { answer: 42 } }}
        extractedInformation={{ answer: 42 }}
        summary="A persisted summary."
      />,
    );
    expect(screen.queryByText("Run outputs")).not.toBeNull();
    expect(screen.getByText("A persisted summary.")).not.toBeNull();
  });

  test("splits the outputs bag into per-block fields, excluding extracted_information", () => {
    render(
      <RunOutputsSection
        {...baseProps}
        outputs={{
          extracted_information: { answer: 42 },
          summary_block: "done",
          data_block: { rows: 2 },
        }}
        extractedInformation={{ answer: 42 }}
      />,
    );

    // Each non-extracted output key becomes its own labeled field.
    expect(screen.queryByText("summary_block")).not.toBeNull();
    expect(screen.queryByText("done")).not.toBeNull();
    // extracted_information keeps its dedicated section and is not repeated as a
    // per-block output field.
    expect(screen.queryByText("extracted_information")).toBeNull();
    // The nested block output renders the collapsible searchable tree.
    expect(screen.queryByPlaceholderText("Search JSON")).not.toBeNull();
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
