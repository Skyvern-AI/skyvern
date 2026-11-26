import { describe, expect, test } from "vitest";

import {
  buildRunGroupCsv,
  csvSafeCell,
  finalOutput,
  safeInputs,
  type GroupCsvRow,
  type GroupRunRead,
} from "./runGroupCsv";
import type { WorkflowBlockType } from "../types/workflowTypes";

const capped = {
  truncated: true,
  reason: "exceeded_max_run_response_value_size",
  original_size_bytes: 3_000_000,
  limit_bytes: 2_097_152,
};

function run(
  outputs: Record<string, unknown> | null,
  lastBlockType: WorkflowBlockType = "code",
): GroupRunRead {
  return {
    status: "completed",
    created_at: "2026-11-01T10:01:00.000000",
    finished_at: "2026-11-01T10:05:00.000000",
    parameters: { "=company": "  =1+1", login: "cred_1", empty: "" },
    outputs,
    workflow: {
      modified_at: "2026-11-01T10:00:00.000000",
      workflow_definition: {
        parameters: [
          { key: "=company", parameter_type: "workflow" },
          {
            key: "login",
            parameter_type: "workflow",
            workflow_parameter_type: "credential_id",
          },
          { key: "empty", parameter_type: "workflow" },
        ],
        blocks: [
          { block_type: "task", output_parameter: { key: "search_output" } },
          {
            block_type: lastBlockType,
            output_parameter: { key: "lookup_output" },
          },
        ],
      },
    },
  };
}

const done = {
  key: "k",
  outcome: "completed",
  run_status: "completed",
} as const;

function row(position: number, read: GroupRunRead | null): GroupCsvRow {
  return {
    position,
    outcome: "Completed",
    workflowRunId: `wr_${position}`,
    inputs: read ? safeInputs(read) : null,
    output: finalOutput(done, read),
  };
}

function parse(csv: string): string[][] {
  const cell = /("(?:[^"]|"")*"|[^,"\r\n]*)(,|\r\n|$)/gy;
  const lines: string[][] = [[]];
  for (const match of csv.matchAll(cell)) {
    const raw = match[1]!;
    lines[lines.length - 1]!.push(
      raw.startsWith('"') ? raw.slice(1, -1).replace(/""/g, '"') : raw,
    );
    if (match[2] === "\r\n") {
      lines.push([]);
    } else if (match[2] === "") {
      break;
    }
  }
  return lines;
}

describe("run group CSV", () => {
  test("escapes formula and control prefixes like csv_safe_cell", () => {
    expect(csvSafeCell("=1+1")).toBe("'=1+1");
    expect(csvSafeCell("  @SUM(A1)")).toBe("'  @SUM(A1)");
    expect(csvSafeCell("\tcell")).toBe("'\tcell");
    expect(csvSafeCell("plain - text")).toBe("plain - text");
  });

  test("takes the final output from the last block of the version the run executed", () => {
    const read = run({
      search_output: "earlier block",
      summary_output: "newer version's last block",
      lookup_output: false,
    });
    expect(finalOutput(done, read)).toEqual({
      state: "recorded",
      value: false,
    });
    expect(finalOutput(done, run({ lookup_output: 0 })).value).toBe(0);
    expect(finalOutput(done, run({ lookup_output: null }))).toEqual({
      state: "recorded",
      value: null,
    });
    expect(finalOutput(done, run({ search_output: "x" })).state).toBe(
      "unavailable",
    );
    expect(finalOutput(done, run({ lookup_output: "[removed]" })).state).toBe(
      "unavailable",
    );
    expect(finalOutput(done, run({ lookup_output: "[redacted]" })).state).toBe(
      "redacted",
    );
    expect(finalOutput(done, run({ lookup_output: capped })).state).toBe(
      "truncated",
    );
    expect(
      finalOutput({ ...done, outcome: "in_progress" }, run(null)).state,
    ).toBe("pending");
    expect(finalOutput({ ...done, key: "[removed]:1" }, run({})).state).toBe(
      "unavailable",
    );
    expect(safeInputs(run(null))).toEqual({ "=company": "  =1+1", empty: "" });
  });

  test("expands both list formats and keeps every other row distinct", () => {
    const csv = buildRunGroupCsv(
      [
        row(
          0,
          run({
            lookup_output: {
              people: [
                { name: "Ada", note: "=1+1" },
                { name: "Lin", note: "ok" },
              ],
              offices: ["Austin"],
              summary: "two lists",
            },
          }),
        ),
        row(
          1,
          run({
            lookup_output: [
              { name: "Kim", note: "" },
              { name: "Sam", note: "0" },
            ],
          }),
        ),
        row(2, run({ lookup_output: [] })),
        row(3, run({ lookup_output: "[redacted]" })),
        row(4, run({ lookup_output: capped })),
        row(5, run({ search_output: "only an earlier block" })),
        { ...row(6, null), output: { state: "omitted" } },
        row(7, run({ lookup_output: null })),
      ],
      "https://app.test",
    ).join("");
    const [header, ...lines] = parse(csv);
    const column = (name: string) => header!.indexOf(name);
    const at = (position: number) =>
      lines.filter((line) => line[column("row")] === String(position + 1));

    expect(header).toEqual([
      "row",
      "outcome",
      "run",
      "input:=company",
      "input:empty",
      "output_state",
      "output",
      "output:summary",
      "list_name",
      "item",
      "item:name",
      "item:note",
    ]);
    expect(
      at(0).map((line) => [
        line[column("list_name")],
        line[column("item:name")] || line[column("item")],
        line[column("output:summary")],
      ]),
    ).toEqual([
      ["people", "Ada", "two lists"],
      ["people", "Lin", "two lists"],
      ["offices", "Austin", "two lists"],
    ]);
    expect(at(0)[0]![column("item:note")]).toBe("'=1+1");
    expect(at(0)[0]![column("input:=company")]).toBe("'  =1+1");
    expect(at(0)[0]![column("input:empty")]).toBe("");
    expect(at(0)[0]![column("run")]).toBe("https://app.test/runs/wr_0");
    expect(at(1).map((line) => line[column("item:name")])).toEqual([
      "Kim",
      "Sam",
    ]);
    expect(at(1)[1]![column("item:note")]).toBe("0");
    expect(at(2).map((line) => line[column("output")])).toEqual(["[]"]);
    expect(at(3)[0]![column("output_state")]).toBe("redacted");
    expect(at(3)[0]![column("output")]).toBe("[redacted]");
    expect(at(4)[0]![column("output_state")]).toBe("truncated");
    expect(at(4)[0]![column("output")]).toBe("[truncated]");
    expect(at(5)[0]![column("output_state")]).toBe("unavailable");
    expect(at(6)[0]![column("output_state")]).toBe("omitted");
    expect(at(6)[0]![column("input:empty")]).toBe("[unavailable]");
    expect(at(7)[0]![column("output_state")]).toBe("recorded");
    expect(at(7)[0]![column("output")]).toBe("null");
    expect(csv).not.toContain("cred_1");
    expect(csv).not.toContain("only an earlier block");
  });

  test("writes a browser task's recorded envelope, not a synthesized extraction, without its signed links", () => {
    const signed = "https://bucket.example/s.png?X-Amz-Signature=sig-3310";
    const taskOutput = {
      task_id: "tsk_1",
      status: "completed",
      extracted_information: { people: [{ name: "Ada" }] },
      task_screenshots: [signed],
      downloaded_file_urls: [signed],
    };
    const read = run({ lookup_output: taskOutput }, "extraction");
    const csv = buildRunGroupCsv([row(0, read)], "https://app.test").join("");
    const [header, line] = parse(csv);

    expect(finalOutput(done, read)).toEqual({
      state: "recorded",
      value: {
        task_id: "tsk_1",
        status: "completed",
        extracted_information: { people: [{ name: "Ada" }] },
      },
    });
    expect(header).not.toContain("list_name");
    expect(line![header!.indexOf("output")]).toContain(
      '"extracted_information":{"people":[{"name":"Ada"}]}',
    );
    expect(csv).not.toContain("sig-3310");
  });

  test("keeps a code block's extracted_information field beside its list and drops a print link", () => {
    const code = finalOutput(
      done,
      run({ lookup_output: { extracted_information: "note", items: [1, 2] } }),
    );
    const csv = buildRunGroupCsv(
      [{ ...row(0, null), output: code }],
      "https://app.test",
    ).join("");
    const [header, ...lines] = parse(csv);

    expect(lines.map((line) => line[header!.indexOf("item")])).toEqual([
      "1",
      "2",
    ]);
    expect(lines[0]![header!.indexOf("output:extracted_information")]).toBe(
      "note",
    );
    expect(
      finalOutput(
        done,
        run(
          {
            lookup_output: {
              filename: "page.pdf",
              artifact_url: "https://bucket.example/p.pdf?sig=1",
              downloaded_files: [{ url: "https://x?sig=2", filename: "p.pdf" }],
            },
          },
          "print_page",
        ),
      ).value,
    ).toEqual({
      filename: "page.pdf",
      downloaded_files: [{ filename: "p.pdf" }],
    });
  });

  test("drops signed links only from block envelopes, inside loop children too", () => {
    const signed = "https://bucket.example/p.pdf?sig=3107";
    const userUrl = "https://user.example/kept-by-code";
    const codeEntry = {
      loop_value: "a",
      output_parameter: { key: "child_code" },
      output_value: { artifact_url: userUrl },
    };
    const loopRun = run({
      lookup_output: [
        [
          {
            loop_value: "a",
            output_parameter: { key: "child_print" },
            output_value: { filename: "p.pdf", artifact_url: signed },
          },
          codeEntry,
        ],
      ],
    });
    loopRun.workflow.workflow_definition.blocks[1] = {
      block_type: "for_loop",
      output_parameter: { key: "lookup_output" },
      loop_blocks: [
        { block_type: "print_page", output_parameter: { key: "child_print" } },
        { block_type: "code", output_parameter: { key: "child_code" } },
      ],
    };

    expect(finalOutput(done, loopRun).value).toEqual([
      [
        {
          loop_value: "a",
          output_parameter: { key: "child_print" },
          output_value: { filename: "p.pdf" },
        },
        codeEntry,
      ],
    ]);
    const code = {
      artifact_url: userUrl,
      downloaded_files: [{ url: userUrl, filename: "f.csv" }],
    };
    expect(finalOutput(done, run({ lookup_output: code })).value).toEqual(code);
    expect(
      finalOutput(
        done,
        run(
          {
            lookup_output: {
              documents: [
                { name: "a", artifact_url: signed },
                { name: "b", artifact_url: null },
              ],
            },
          },
          "split_pdf",
        ),
      ).value,
    ).toEqual({
      documents: [{ name: "a" }, { name: "b", artifact_url: null }],
    });
  });

  test("hides the output and inputs of a run whose workflow was edited in place after it finished", () => {
    const edited = run({ lookup_output: "found before the edit" });
    edited.workflow.modified_at = "2026-11-01T10:06:00.000000";
    edited.parameters.removed = "old value";
    edited.workflow.workflow_definition.parameters = [
      {
        key: "=company",
        parameter_type: "workflow",
        workflow_parameter_type: "credential_id",
      },
      { key: "empty", parameter_type: "workflow" },
    ];

    expect(finalOutput(done, edited)).toEqual({
      state: "unavailable",
      reason: "definition_changed",
    });
    expect(safeInputs(edited)).toBeNull();
    const csv = buildRunGroupCsv([row(0, edited)], "https://app.test").join("");
    const [header, line] = parse(csv);
    expect(line![header!.indexOf("output_state")]).toBe("unavailable");
    expect(line![header!.indexOf("output")]).toBe("definition_changed");
    expect(csv).not.toContain("found before the edit");
    expect(csv).not.toContain("old value");
  });

  test("hides a credential input whose key was retyped to a string after the run was created", () => {
    const retyped = run({ lookup_output: "found" });
    retyped.workflow.modified_at = "2026-11-01T10:02:00.000000";
    retyped.workflow.workflow_definition.parameters[1] = {
      key: "login",
      parameter_type: "workflow",
      workflow_parameter_type: "string",
    };

    expect(safeInputs(retyped)).toBeNull();
    const csv = buildRunGroupCsv(
      [row(0, retyped), row(1, run({ lookup_output: "found" }))],
      "https://app.test",
    ).join("");
    expect(csv).not.toContain("cred_1");
    const [header, first, second] = parse(csv);
    expect(first![header!.indexOf("input:=company")]).toBe("[unavailable]");
    expect(second![header!.indexOf("input:=company")]).toBe("'  =1+1");
  });

  test("drops signed links from a task and a loop holding a task, and leaves a code block's output as recorded", () => {
    const signed = "https://bucket.example/s.png?X-Amz-Signature=sig-4410";
    const envelope = {
      status: "completed",
      extracted_information: { total: 3 },
      task_screenshots: [signed],
      downloaded_file_urls: [signed],
    };
    const kept = { status: "completed", extracted_information: { total: 3 } };
    const task = finalOutput(
      done,
      run({ search_output: "earlier", lookup_output: envelope }, "task"),
    );
    expect(task).toEqual({ state: "recorded", value: kept });

    const entry = { loop_value: "a", output_parameter: { key: "child_task" } };
    const loopRun = run({
      lookup_output: [[{ ...entry, output_value: envelope }]],
    });
    loopRun.workflow.workflow_definition.blocks[1] = {
      block_type: "for_loop",
      output_parameter: { key: "lookup_output" },
      loop_blocks: [
        { block_type: "task", output_parameter: { key: "child_task" } },
      ],
    };
    expect(finalOutput(done, loopRun)).toEqual({
      state: "recorded",
      value: [[{ ...entry, output_value: kept }]],
    });

    const code = { artifact_url: signed };
    expect(finalOutput(done, run({ lookup_output: code }, "code"))).toEqual({
      state: "recorded",
      value: code,
    });
    expect(JSON.stringify([task, finalOutput(done, loopRun)])).not.toContain(
      "sig-4410",
    );
  });

  test("writes an empty named list as [] on every line of its row", () => {
    const csv = buildRunGroupCsv(
      [
        row(
          0,
          run({
            lookup_output: {
              people: [{ name: "Ada" }, { name: "Lin" }],
              offices: [],
            },
          }),
        ),
        row(1, run({ lookup_output: { people: [], offices: [] } })),
      ],
      "https://app.test",
    ).join("");
    const [header, ...lines] = parse(csv);
    const column = (name: string) => header!.indexOf(name);

    expect(
      lines.map((line) => [
        line[column("row")],
        line[column("item:name")],
        line[column("output:offices")],
        line[column("output:people")],
      ]),
    ).toEqual([
      ["1", "Ada", "[]", ""],
      ["1", "Lin", "[]", ""],
      ["2", "", "[]", "[]"],
    ]);
  });

  test("exports a list too long to spread into one call", () => {
    const items = Array.from({ length: 150_000 }, (_, index) => index);
    const parts = buildRunGroupCsv(
      [row(0, run({ lookup_output: { items } }))],
      "https://app.test",
    );

    expect(parts).toHaveLength(150_001);
    expect(parts[150_000]).toMatch(/,items,149999$/);
  });
});
