import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

import { TestRunOutputCard } from "./TestRunOutputCard";

const factsWith = (extracted: unknown) => ({
  workflow_run_id: "wr-1",
  status: "completed",
  available: true,
  failure_reason: null,
  outputs: [
    {
      output_parameter_id: "op-1",
      value: {
        task_id: "tsk_hidden",
        status: "completed",
        extracted_information: extracted,
      },
    },
  ],
});

describe("TestRunOutputCard", () => {
  it.each([
    ["an object", { price: "12.50" }, "12.50"],
    ["a list", [{ name: "First row" }, { name: "Second row" }], "Second row"],
    ["a string", "Only one line of text", "Only one line of text"],
  ])(
    "shows an extraction that is %s without opening the raw output",
    (_, extracted, visible) => {
      render(
        <TestRunOutputCard facts={factsWith(extracted)} workflow={null} />,
      );

      const card = screen.getByTestId("proposal-run-facts");
      expect(card.textContent).toContain(visible);
      expect(card.textContent).not.toContain("tsk_hidden");
    },
  );

  it.each([
    ["failed", true],
    ["canceled", false],
    ["terminated", false],
    ["timed_out", false],
  ])("marks a %s run as failed only when it failed", (status, marked) => {
    render(
      <TestRunOutputCard
        facts={{ ...factsWith({ price: "1" }), status }}
        workflow={null}
      />,
    );
    const card = screen.getByTestId("proposal-run-facts");
    expect(card.textContent).toContain(status);
    expect(
      card.querySelector(".text-destructive svg, svg.text-destructive") !==
        null,
    ).toBe(marked);
  });

  it("orders outputs by reading order, a conditional's branch before its merge block", () => {
    const block = (label: string, extra: Record<string, unknown> = {}) => ({
      label,
      block_type: "task",
      next_block_label: null,
      output_parameter: { output_parameter_id: `op-${label}` },
      ...extra,
    });
    // The editor saves branch children after the merge block in the array.
    const workflow = {
      workflow_definition: {
        blocks: [
          block("pick_path", {
            block_type: "conditional",
            next_block_label: "save_total",
            branch_conditions: [{ next_block_label: "read_price" }],
          }),
          block("save_total"),
          block("read_price", { next_block_label: "save_total" }),
        ],
      },
    } as unknown as WorkflowApiResponse;
    const output = (label: string, value: string) => ({
      output_parameter_id: `op-${label}`,
      value: { extracted_information: { [label]: value } },
    });
    render(
      <TestRunOutputCard
        facts={{
          ...factsWith(null),
          outputs: [
            output("save_total", "final"),
            output("read_price", "early"),
          ],
        }}
        workflow={workflow}
      />,
    );
    const text = screen.getByTestId("proposal-run-facts").textContent ?? "";
    // The last block in reading order is the emphasized result, rendered first.
    expect(text.indexOf("final")).toBeLessThan(text.indexOf("early"));
  });
});
