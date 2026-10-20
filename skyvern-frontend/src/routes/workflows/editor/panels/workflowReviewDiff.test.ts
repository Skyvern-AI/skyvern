import { describe, expect, test } from "vitest";

import type { WorkflowVersion } from "../../hooks/useWorkflowVersionsQuery";
import type { WorkflowBlock } from "../../types/workflowTypes";
import { apiWorkflowToSettings } from "../apiWorkflowToSettings";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { getElements } from "../workflowEditorUtils";
import {
  diffWords,
  diffWorkflowVersions,
  foldUnchangedRuns,
} from "./workflowReviewDiff";

let idCounter = 0;

// Every call mints fresh ids and timestamps, the way a rehydrated Copilot
// proposal does for blocks whose content did not change.
function block(
  label: string,
  block_type: string,
  fields: Record<string, unknown> = {},
): WorkflowBlock {
  idCounter += 1;
  return {
    label,
    block_type,
    continue_on_failure: false,
    model: null,
    output_parameter: {
      parameter_type: "output",
      key: `${label}_output`,
      output_parameter_id: `op_${idCounter}`,
      workflow_id: `w_${idCounter}`,
      created_at: `2026-09-0${(idCounter % 9) + 1}T00:00:00Z`,
      modified_at: `2026-09-0${(idCounter % 9) + 1}T00:00:00Z`,
      deleted_at: null,
      description: null,
    },
    ...fields,
  } as unknown as WorkflowBlock;
}

function version(
  blocks: Array<WorkflowBlock>,
  overrides: Partial<WorkflowVersion> = {},
  parameters: Array<unknown> = [],
): WorkflowVersion {
  return {
    title: "wf",
    workflow_definition: { version: 2, parameters, blocks },
    ...overrides,
  } as unknown as WorkflowVersion;
}

// Reads run order off the rendered canvas, the way a reviewer sees it: the
// top-level chain, or the chain inside the loop labelled `insideLoop`.
function canvasOrder(
  blocks: Array<WorkflowBlock>,
  v: WorkflowVersion,
  insideLoop?: string,
) {
  const { nodes, edges } = getElements(blocks, apiWorkflowToSettings(v), false);
  const parentId = insideLoop
    ? nodes.find(
        (node) => isWorkflowBlockNode(node) && node.data.label === insideLoop,
      )?.id
    : undefined;
  const level = nodes.filter((node) => node.parentId === parentId);
  const byId = new Map(level.map((node) => [node.id, node]));
  const order: Array<string> = [];
  let cursor: AppNode | undefined = level.find((node) => node.type === "start");
  const seen = new Set<string>();
  while (cursor && !seen.has(cursor.id)) {
    seen.add(cursor.id);
    if (isWorkflowBlockNode(cursor)) order.push(cursor.data.label);
    const edge = edges.find(
      (e) => e.source === cursor!.id && byId.has(e.target),
    );
    cursor = edge ? byId.get(edge.target) : undefined;
  }
  return order;
}

function conditional(
  label: string,
  targets: Array<string>,
  next: string | null,
): WorkflowBlock {
  return block(label, "conditional", {
    next_block_label: next,
    branch_conditions: targets.map((target, index) => ({
      id: `${label}_${index}_${++idCounter}`,
      criteria:
        index === targets.length - 1
          ? null
          : {
              criteria_type: "jinja2_template",
              expression: `{{ n == ${index} }}`,
            },
      next_block_label: target,
      description: null,
      is_default: index === targets.length - 1,
    })),
  });
}

function step(label: string, next: string | null, goal = label) {
  return block(label, "navigation", {
    navigation_goal: goal,
    next_block_label: next,
  });
}

function branchesOf(v: WorkflowVersion) {
  return (
    v.workflow_definition.blocks[0] as unknown as {
      branch_conditions: Array<{ is_default: boolean; criteria: unknown }>;
    }
  ).branch_conditions;
}

// Where each block lands on the canvas: hidden or not, and which conditional
// branch holds it.
function placement(blocks: Array<WorkflowBlock>, v: WorkflowVersion) {
  const { nodes } = getElements(blocks, apiWorkflowToSettings(v), false);
  return new Map(
    nodes.flatMap((node) =>
      isWorkflowBlockNode(node)
        ? [
            [
              node.data.label,
              {
                hidden: Boolean(node.hidden),
                branch:
                  (node.data as { conditionalBranchId?: string | null })
                    .conditionalBranchId ?? null,
              },
            ] as const,
          ]
        : [],
    ),
  );
}

function vendorInvoicesBefore() {
  return [
    block("open_vendor_portal", "goto_url", { url: "https://vendor.test" }),
    block("sign_in", "login", { navigation_goal: "Sign in" }),
    block("go_to_invoices", "navigation", {
      navigation_goal: "Open the Billing tab and click Invoices.",
      max_steps_per_run: 10,
    }),
    block("notify_finance", "send_email", {
      subject: "Invoices downloaded",
      recipients: ["finance@example.com"],
      file_attachments: [],
    }),
    block("upload_to_drive", "upload_to_s3", { path: "invoices" }),
  ];
}

function vendorInvoicesAfter() {
  return [
    block("open_vendor_portal", "goto_url", { url: "https://vendor.test" }),
    block("sign_in", "login", { navigation_goal: "Sign in" }),
    block("go_to_invoices", "navigation", {
      navigation_goal:
        "Open the Billing tab, click Invoices, and filter to {{ billing_month }}.",
      max_steps_per_run: 15,
    }),
    block("extract_invoice_rows", "extraction", {
      data_extraction_goal: "Extract every invoice row.",
    }),
    block("each_invoice", "for_loop", {
      loop_variable_reference: "extract_invoice_rows.output",
      loop_blocks: [
        block("download_invoice_pdf", "file_download", {
          navigation_goal: "Click the PDF link.",
        }),
      ],
    }),
    block("upload_to_drive", "upload_to_s3", { path: "invoices" }),
  ];
}

describe("diffWorkflowVersions", () => {
  test("an edit renders one canvas with the removed block where it ran", () => {
    const after = version(vendorInvoicesAfter());
    const diff = diffWorkflowVersions(version(vendorInvoicesBefore()), after);

    expect(canvasOrder(diff.mergedBlocks, after)).toEqual([
      "open_vendor_portal",
      "sign_in",
      "go_to_invoices",
      "notify_finance",
      "extract_invoice_rows",
      "each_invoice",
      "upload_to_drive",
    ]);
    const status = (label: string) => diff.merged.get(label)?.status;
    expect(status("open_vendor_portal")).toBe("unchanged");
    expect(status("sign_in")).toBe("unchanged");
    expect(status("go_to_invoices")).toBe("changed");
    expect(status("extract_invoice_rows")).toBe("new");
    expect(status("download_invoice_pdf")).toBe("new");
    expect(status("notify_finance")).toBe("removed");
    expect(diff.merged.get("go_to_invoices")?.changes).toEqual([
      {
        key: "navigation_goal",
        label: "Navigation goal",
        before: "Open the Billing tab and click Invoices.",
        after:
          "Open the Billing tab, click Invoices, and filter to {{ billing_month }}.",
      },
      {
        key: "max_steps_per_run",
        label: "Max steps",
        before: "10",
        after: "15",
      },
    ]);
    expect(diff.counts).toEqual({
      new: 3,
      changed: 1,
      removed: 1,
      unchanged: 3,
    });
    expect(diff.changeOrder).toEqual([
      "go_to_invoices",
      "notify_finance",
      "extract_invoice_rows",
      "each_invoice",
      "download_invoice_pdf",
    ]);
    expect(diff.isNewWorkflow).toBe(false);
  });

  test("a loop's children are compared one by one, removed ones included", () => {
    const loop = (children: Array<WorkflowBlock>) =>
      block("each_row", "for_loop", {
        loop_variable_reference: "rows",
        loop_blocks: children,
      });
    const openRow = () =>
      block("open_row", "navigation", { navigation_goal: "Open row" });
    const after = version([
      loop([
        openRow(),
        block("save_row", "navigation", { navigation_goal: "Save it twice" }),
      ]),
    ]);
    const diff = diffWorkflowVersions(
      version([
        loop([
          openRow(),
          block("check_row", "validation", { complete_criterion: "Row open" }),
          block("save_row", "navigation", { navigation_goal: "Save it" }),
        ]),
      ]),
      after,
    );

    expect(canvasOrder(diff.mergedBlocks, after, "each_row")).toEqual([
      "open_row",
      "check_row",
      "save_row",
    ]);
    expect(diff.merged.get("each_row")).toMatchObject({
      status: "unchanged",
      containsChanges: true,
    });
    expect(diff.merged.get("open_row")?.status).toBe("unchanged");
    expect(diff.merged.get("check_row")?.status).toBe("removed");
    expect(diff.merged.get("save_row")?.status).toBe("changed");
    expect(diff.changeOrder).toEqual(["check_row", "save_row"]);
  });

  test("removed first and last blocks stay at the ends, and a type change keeps both cards", () => {
    const after = version([
      block("fill_form", "navigation", { navigation_goal: "Fill" }),
      block("summary", "extraction", { data_extraction_goal: "Read it" }),
    ]);
    const diff = diffWorkflowVersions(
      version([
        block("open_site", "goto_url", { url: "https://a.test" }),
        block("fill_form", "navigation", { navigation_goal: "Fill" }),
        block("summary", "text_prompt", { prompt: "Summarize" }),
      ]),
      after,
    );

    expect(canvasOrder(diff.mergedBlocks, after)).toEqual([
      "open_site",
      "fill_form",
      "summary (removed)",
      "summary",
    ]);
    expect(diff.merged.get("summary")?.status).toBe("new");

    const unfinalized = version([step("a", "b"), step("b", null)]);
    const finalized = version([
      step("cleanup", null),
      step("a", "b"),
      step("b", null),
    ]);
    finalized.workflow_definition.finally_block_label = "cleanup";
    const finallyRemoved = diffWorkflowVersions(finalized, unfinalized);
    expect(canvasOrder(finallyRemoved.mergedBlocks, unfinalized)).toEqual([
      "a",
      "b",
      "cleanup",
    ]);
    expect(diff.merged.get("summary (removed)")?.status).toBe("removed");
    expect(diff.before.get("summary")?.status).toBe("removed");
  });

  test("run order, not storage order, places removed blocks", () => {
    const after = version([step("c", null, "edited"), step("a", "c")]);
    const removal = diffWorkflowVersions(
      version([step("c", null), step("a", "b"), step("b", "c")]),
      after,
    );
    expect(canvasOrder(removal.mergedBlocks, after)).toEqual(["a", "b", "c"]);
    expect(removal.changeOrder).toEqual(["b", "c"]);
  });

  test("removed blocks around a conditional keep their branch and never hide kept blocks", () => {
    const flattened = version([
      step("a", "x"),
      step("x", "z"),
      step("z", "w", "edited"),
      step("w", null),
    ]);
    const unwrap = diffWorkflowVersions(
      version([
        step("a", "c"),
        conditional("c", ["x", "y"], "z"),
        step("x", "z"),
        step("y", "z"),
        step("z", "w"),
        step("w", null),
      ]),
      flattened,
    );
    expect(canvasOrder(unwrap.mergedBlocks, flattened)).toEqual([
      "a",
      "c",
      "x",
      "z",
      "w",
    ]);
    const unwrapped = placement(unwrap.mergedBlocks, flattened);
    expect(unwrapped.get("z")?.hidden).toBe(false);
    expect(unwrapped.get("y")?.branch).not.toBeNull();

    const inlined = version([
      step("a", "x"),
      step("x", "y"),
      step("y", "z"),
      step("z", null),
    ]);
    const stale = diffWorkflowVersions(
      version([
        step("a", "c"),
        conditional("c", ["x", "y"], "z"),
        step("x", "z"),
        step("y", "z"),
        step("z", null),
      ]),
      inlined,
    );
    expect(canvasOrder(stale.mergedBlocks, inlined)).toEqual([
      "a",
      "c",
      "x",
      "y",
      "z",
    ]);

    const trimmed = version([
      conditional("c", ["x", "y"], "z"),
      step("x", "z"),
      step("y", "z"),
      step("z", null),
    ]);
    const tail = diffWorkflowVersions(
      version([
        conditional("c", ["x", "y"], "z"),
        step("x", "r"),
        step("r", "z"),
        step("y", "z"),
        step("z", null),
      ]),
      trimmed,
    );
    const branches = placement(tail.mergedBlocks, trimmed);
    expect(branches.get("r")?.branch).toBe(branches.get("x")?.branch);
    expect(branches.get("r")?.hidden).toBe(false);
    expect(tail.merged.get("c")?.status).toBe("unchanged");

    const rejoined = version([
      conditional("c", ["n", "y"], "z"),
      step("n", "x"),
      step("x", "z"),
      step("y", "z"),
      step("z", null),
    ]);
    const merge = diffWorkflowVersions(
      version([
        conditional("c", ["x", "y"], "r"),
        step("x", "r"),
        step("y", "r"),
        step("r", "z"),
        step("z", null),
      ]),
      rejoined,
    );
    expect(canvasOrder(merge.mergedBlocks, rejoined)).toEqual(["c", "r", "z"]);
    expect(placement(merge.mergedBlocks, rejoined).get("z")?.hidden).toBe(
      false,
    );
    expect(merge.merged.get("c")?.status).toBe("unchanged");

    const narrowed = version([
      conditional("c", ["x", "y"], "z"),
      step("x", "z"),
      step("y", "z"),
      step("z", null),
    ]);
    const dropped = diffWorkflowVersions(
      version([
        conditional("c", ["x", "b", "y"], "z"),
        step("x", "z"),
        step("b", "z"),
        step("y", "z"),
        step("z", null),
      ]),
      narrowed,
    );
    expect(canvasOrder(dropped.mergedBlocks, narrowed)).toEqual(["c", "z"]);
    expect(
      placement(dropped.mergedBlocks, narrowed).get("b")?.branch,
    ).not.toBeNull();

    const elseless = version([
      conditional("c", ["x", "y"], "z"),
      step("x", "z"),
      step("y", "z"),
      step("z", null),
    ]);
    const lastBranch = branchesOf(elseless)[1]!;
    lastBranch.is_default = false;
    lastBranch.criteria = {
      criteria_type: "jinja2_template",
      expression: "{{ n == 1 }}",
    };
    const droppedElse = diffWorkflowVersions(
      version([
        conditional("c", ["x", "y", "e"], "z"),
        step("x", "z"),
        step("y", "z"),
        step("e", "z"),
        step("z", null),
      ]),
      elseless,
    );
    expect(canvasOrder(droppedElse.mergedBlocks, elseless)).toEqual(["c", "z"]);
    expect(
      placement(droppedElse.mergedBlocks, elseless).get("e")?.branch,
    ).not.toBeNull();

    const widened = version([
      conditional("c", ["x", "w", "y"], "z"),
      step("x", "z"),
      step("w", "z"),
      step("y", "z"),
      step("z", null),
    ]);
    branchesOf(widened)[2]!.criteria = {
      criteria_type: "jinja2_template",
      expression: "",
    };
    const elseReplaced = diffWorkflowVersions(
      version([
        conditional("c", ["x", "e"], "z"),
        step("x", "z"),
        step("e", "z"),
        step("z", null),
      ]),
      widened,
    );
    expect(canvasOrder(elseReplaced.mergedBlocks, widened)).toEqual(["c", "z"]);

    const branchesOnly = (targets: Array<string>) =>
      version([
        conditional("c", targets, "z"),
        step("x", "z"),
        step("y", "z"),
        step("z", null),
      ]);
    const swap = diffWorkflowVersions(
      branchesOnly(["x", "y"]),
      branchesOnly(["y", "x"]),
    );
    expect(swap.merged.get("c")?.changes).toEqual([
      {
        key: "__branch_routes",
        label: "Branch routes",
        before: "Branch 1 → x, Else → y",
        after: "Branch 1 → y, Else → x",
      },
    ]);
  });

  test("an edited input is reviewed on Start, not on each block that reads it", () => {
    const input = (description: string) => ({
      parameter_type: "workflow",
      key: "vendor",
      workflow_parameter_type: "string",
      workflow_parameter_id: `wp_${++idCounter}`,
      default_value: null,
      description,
    });
    const reader = (description: string) =>
      block("open_portal", "navigation", {
        navigation_goal: "Open {{ vendor }}",
        parameters: [input(description)],
      });
    const diff = diffWorkflowVersions(
      version([reader("old")], {}, [input("old")]),
      version([reader("new")], {}, [input("new")]),
    );

    expect(diff.merged.get("open_portal")?.status).toBe("unchanged");
    expect(diff.inputs).toMatchObject([{ key: "vendor", status: "changed" }]);
  });

  test("input and setting changes are reported without serializer noise", () => {
    const workflowInput = (
      key: string,
      extra: Record<string, unknown> = {},
    ) => ({
      parameter_type: "workflow",
      key,
      workflow_parameter_type: "string",
      workflow_parameter_id: `wp_${key}_${++idCounter}`,
      default_value: null,
      description: null,
      ...extra,
    });
    const diff = diffWorkflowVersions(
      version(
        [],
        {
          max_elapsed_time_minutes: 15,
          extra_http_headers: { a: "1", b: "2" },
          run_with: "agent",
          code_version: null,
        },
        [workflowInput("vendor")],
      ),
      version(
        [],
        {
          max_elapsed_time_minutes: 25,
          extra_http_headers: { b: "2", a: "1" },
          run_with: "agent",
          code_version: 2,
        },
        [workflowInput("vendor"), workflowInput("billing_month")],
      ),
    );

    expect(diff.inputs).toEqual([
      { key: "billing_month", status: "new", detail: "string", changes: [] },
    ]);
    expect(diff.settings).toEqual([
      {
        key: "maxElapsedTimeMinutes",
        label: "Max run time (minutes)",
        before: "15",
        after: "25",
      },
    ]);
  });

  test("secret values are hidden while their change still shows", () => {
    const upload = (password: string, token: string) =>
      block("upload_report", "file_upload", {
        sftp_password: password,
        headers: {
          Authorization: `Bearer ${token}`,
          "X-CSRF-Token": `csrf-${token}`,
          Accept: "text/csv",
        },
      });
    const diff = diffWorkflowVersions(
      version([upload("old-pass", "old-token")]),
      version([upload("new-pass", "new-token")]),
    );

    const changes = diff.merged.get("upload_report")?.changes ?? [];
    expect(changes.map((change) => change.key).sort()).toEqual([
      "headers",
      "sftp_password",
    ]);
    const shown = JSON.stringify(changes);
    for (const secret of ["old-pass", "new-pass", "old-token", "new-token"]) {
      expect(shown).not.toContain(secret);
    }
    expect(shown).toContain("text/csv");

    const tokenInput = (value: string) => ({
      parameter_type: "workflow",
      key: "api_token",
      workflow_parameter_type: "string",
      workflow_parameter_id: `wp_${++idCounter}`,
      default_value: value,
      description: null,
    });
    const settingsFor = (headers: unknown, token: string) =>
      version(
        [],
        {
          extra_http_headers: headers as unknown as Record<string, string>,
        },
        [tokenInput(token)],
      );
    const inputs = diffWorkflowVersions(
      settingsFor('{"Authorization": "Bearer old-token"', "old-default"),
      settingsFor('{"Authorization": "Bearer new-token"', "new-default"),
    );
    const startShown = JSON.stringify([...inputs.inputs, ...inputs.settings]);
    expect(inputs.inputs).toMatchObject([
      { key: "api_token", status: "changed" },
    ]);
    for (const secret of [
      "old-default",
      "new-default",
      "old-token",
      "new-token",
    ]) {
      expect(startShown).not.toContain(secret);
    }

    const sessionHeaders = (token: string) =>
      settingsFor({ "X-Amz-Security-Token": token }, "same-default");
    const cdp = diffWorkflowVersions(
      sessionHeaders("old-session"),
      sessionHeaders("new-session"),
    );
    expect(cdp.settings.map((change) => change.key)).toEqual([
      "extraHttpHeaders",
    ]);
    const cdpShown = JSON.stringify(cdp.settings);
    expect(cdpShown).not.toContain("old-session");
    expect(cdpShown).not.toContain("new-session");
  });

  test("a title-only proposal is reported, not read as no changes", () => {
    const diff = diffWorkflowVersions(
      version([], { title: "Download invoices" }),
      version([], { title: "Download vendor invoices" }),
    );

    expect(diff.settings).toEqual([
      {
        key: "title",
        label: "Title",
        before: "Download invoices",
        after: "Download vendor invoices",
      },
    ]);
  });

  test("a brand-new workflow is flagged as new with every block new", () => {
    const diff = diffWorkflowVersions(
      version([]),
      version([
        block("open_store", "goto_url", { url: "https://shop.test" }),
        block("find_order", "navigation", { navigation_goal: "Find it" }),
      ]),
    );

    expect(diff.isNewWorkflow).toBe(true);
    expect(diff.counts).toEqual({
      new: 2,
      changed: 0,
      removed: 0,
      unchanged: 0,
    });
  });
});

describe("foldUnchangedRuns", () => {
  test("folds every run of unchanged blocks and keeps the chain intact", () => {
    const labels = ["a", "b", "c", "d", "e", "f"];
    const make = (edited: Array<string>) =>
      labels.map((label) =>
        block(label, "navigation", {
          navigation_goal: edited.includes(label) ? "edited" : label,
        }),
      );
    const after = version(make(["a", "e"]));
    const diff = diffWorkflowVersions(version(make([])), after);

    const folded = foldUnchangedRuns(
      diff.mergedBlocks,
      diff.merged,
      new Set(),
      null,
    );
    expect(canvasOrder(folded.blocks, after)).toEqual([
      "a",
      "__review_fold__b",
      "e",
      "__review_fold__f",
    ]);
    expect([...folded.folds.values()]).toEqual([
      { key: "b", count: 3 },
      { key: "f", count: 1 },
    ]);

    const expanded = foldUnchangedRuns(
      diff.mergedBlocks,
      diff.merged,
      new Set(["b", "f"]),
      null,
    );
    expect(canvasOrder(expanded.blocks, after)).toEqual(labels);

    const stored = (goal: string) =>
      version([
        step("c", "d"),
        step("a", "b"),
        step("b", "c"),
        step("d", null, goal),
      ]);
    const shuffled = stored("edited");
    const reordered = diffWorkflowVersions(stored("d"), shuffled);
    const foldedInRunOrder = foldUnchangedRuns(
      reordered.mergedBlocks,
      reordered.merged,
      new Set(),
      null,
    );
    expect(canvasOrder(foldedInRunOrder.blocks, shuffled)).toEqual([
      "__review_fold__a",
      "d",
    ]);
    expect([...foldedInRunOrder.folds.values()]).toEqual([
      { key: "a", count: 3 },
    ]);
  });
});

describe("diffWords", () => {
  test("a one-word edit to a long prompt highlights only that word", () => {
    const words = Array.from({ length: 150 }, (_, index) => `word${index}`);
    const before = words.join(" ");
    const after = words
      .map((word, index) => (index === 75 ? "edited" : word))
      .join(" ");

    const diff = diffWords(before, after);

    expect(diff.before.filter((s) => s.changed).map((s) => s.text)).toEqual([
      "word75",
    ]);
    expect(diff.after.filter((s) => s.changed).map((s) => s.text)).toEqual([
      "edited",
    ]);
  });
});
