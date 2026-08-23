// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
    expect(
      screen.getAllByRole("button", { name: "Search JSON" }).length,
    ).toBeGreaterThan(0);
  });

  test("renders code-only outputs with no extracted information, files, or errors", () => {
    render(
      <RunOutputsSection
        {...baseProps}
        outputs={{
          get_stars_output: { star_count: 22600, evidence_text: "22.6k stars" },
          extracted_information: [],
        }}
      />,
    );

    // The early return must not swallow a code-only run's returned values.
    expect(screen.getByText("Run outputs")).not.toBeNull();
    expect(screen.getAllByText("get_stars_output").length).toBeGreaterThan(0);
    expect(
      screen.getAllByRole("button", { name: "Search JSON" }).length,
    ).toBeGreaterThan(0);
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

describe("RunOutputsSection output search", () => {
  test("each block's explorer searches itself, behind its own toggle", () => {
    render(
      <RunOutputsSection
        {...baseProps}
        outputs={{
          block_a_output: { invoice_total: 120 },
          block_b_output: { shipping_carrier: "ups" },
        }}
      />,
    );
    // No field is open until asked for, so n blocks do not stack n inputs.
    expect(screen.queryByPlaceholderText("Search JSON")).toBeNull();
    const toggles = screen.getAllByRole("button", { name: "Search JSON" });
    expect(toggles).toHaveLength(2);

    fireEvent.click(toggles[1]!);
    fireEvent.change(screen.getByPlaceholderText("Search JSON"), {
      target: { value: "120" },
    });

    // Scoped: block_b empties on a value only block_a holds; block_a is untouched.
    expect(screen.queryAllByText(/^shipping_carrier/)).toHaveLength(0);
    expect(screen.getAllByText(/^invoice_total/).length).toBeGreaterThan(0);
  });
});
