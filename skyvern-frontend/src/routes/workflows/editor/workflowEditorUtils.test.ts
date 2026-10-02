import { describe, expect, test } from "vitest";

import { ProxyLocation, RunEngine } from "@/api/types";

import type { AppNode } from "./nodes";
import type {
  WorkflowApiResponse,
  WorkflowBlock,
  WorkflowSettings,
} from "../types/workflowTypes";
import {
  blockRunErrors,
  convert,
  createNode,
  getElements,
  getWorkflowBlocks,
  getWorkflowErrors,
  pendingGoalChangesOf,
  withGoalUndoRecordsFrom,
} from "./workflowEditorUtils";
import { collectKnownErrorCodes } from "./nodes/StartNode/retryPolicyUtils";
import { terminateNodeDefaultData } from "./nodes/TerminateNode/types";

function minimalTaskBlock(overrides: { engine?: RunEngine } = {}) {
  return {
    block_type: "task",
    label: "b1",
    continue_on_failure: false,
    next_loop_on_failure: false,
    model: null,
    ignore_workflow_system_prompt: false,
    url: null,
    navigation_goal: null,
    data_extraction_goal: null,
    data_schema: null,
    error_code_mapping: null,
    complete_on_download: false,
    download_suffix: null,
    max_retries: null,
    max_steps_per_run: null,
    parameters: [],
    totp_identifier: null,
    totp_verification_url: null,
    disable_cache: false,
    complete_criterion: null,
    terminate_criterion: null,
    include_action_history_in_verification: false,
    ...overrides,
  } as unknown as WorkflowBlock;
}

const SETTINGS: WorkflowSettings = {
  proxyLocation: ProxyLocation.Residential,
  webhookCallbackUrl: null,
  totpVerificationUrl: null,
  totpIdentifier: null,
  adaptiveCaching: false,
  generateScriptOnTerminal: false,
  persistBrowserSession: false,
  reuseBrowserSession: false,
  pinSavedSessionIp: false,
  browserProfileId: null,
  browserProfileKey: null,
  model: null,
  maxScreenshotScrolls: null,
  maxElapsedTimeMinutes: null,
  extraHttpHeaders: null,
  cdpConnectHeaders: null,
  runWith: "agent",
  codeVersion: null,
  scriptCacheKey: null,
  aiFallback: true,
  maskSecrets: false,
  runSequentially: false,
  sequentialKey: null,
  finallyBlockLabel: null,
  workflowSystemPrompt: null,
  errorCodeMapping: null,
  retryPolicy: null,
};

function codeBlock(goalNeedsRegeneration: boolean | null): AppNode {
  return {
    id: "cb1",
    type: "codeBlock",
    position: { x: 0, y: 0 },
    data: {
      debuggable: true,
      editable: true,
      label: "lookup_invoice",
      code: "print(1)",
      continueOnFailure: false,
      parameterKeys: [],
      errorCodeMapping: "null",
      prompt: "Download last month's invoice",
      steps: null,
      dataSchema: "null",
      userOwnedGoal: true,
      goalNeedsRegeneration,
      codeEditedByHand: null,
      model: null,
    },
  } as AppNode;
}

describe("getWorkflowErrors", () => {
  test("validates a new Terminate node without throwing", () => {
    const node = {
      id: "terminate-1",
      type: "terminate",
      position: { x: 0, y: 0 },
      data: { ...terminateNodeDefaultData, label: "Stop" },
    } as AppNode;

    expect(getWorkflowErrors([node])).toEqual(["Stop: Reason is required."]);
  });

  test("refuses to save a code block whose edited Goal has not been rebuilt into code", () => {
    const errors = getWorkflowErrors([codeBlock(true)]);

    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("lookup_invoice");
  });

  test("a rebuild flag on a Goal no person owns blocks nothing, as on the backend", () => {
    const apiAuthored = {
      ...codeBlock(true),
      data: { ...codeBlock(true).data, userOwnedGoal: null },
    } as AppNode;

    expect(getWorkflowErrors([apiAuthored])).toEqual([]);
  });

  test("a hand code edit holds no save and survives load, save and export", () => {
    const stored = {
      block_type: "code",
      label: "read_total",
      code: "return {'total': 1}",
      parameters: [],
      error_code_mapping: null,
      prompt: "Read the total",
      user_owned_goal: true,
      code_edited_by_hand: true,
      continue_on_failure: false,
      next_loop_on_failure: false,
      model: null,
    } as unknown as WorkflowBlock;
    const { nodes, edges } = getElements([stored], SETTINGS, true);
    const source = {
      title: "Source",
      description: null,
      is_saved_task: false,
      status: null,
      run_with: "agent",
      workflow_definition: { parameters: [], blocks: [stored] },
    } as unknown as WorkflowApiResponse;

    expect(getWorkflowErrors(nodes)).toEqual([]);
    expect(getWorkflowBlocks(nodes, edges)[0]).toMatchObject({
      code_edited_by_hand: true,
    });
    expect(convert(source).workflow_definition.blocks[0]).toMatchObject({
      code_edited_by_hand: true,
    });
  });

  test("saves once the code has been rebuilt from the Goal", () => {
    expect(getWorkflowErrors([codeBlock(false)])).toEqual([]);
    expect(getWorkflowErrors([codeBlock(null)])).toEqual([]);
  });
});

describe("withGoalUndoRecordsFrom", () => {
  const edited = {
    ...codeBlock(true),
    data: {
      ...codeBlock(true).data,
      goalBeforeEdit: {
        prompt: "Download the latest statement",
        userOwnedGoal: null,
        goalNeedsRegeneration: null,
      },
    },
  } as AppNode;

  test("a draft that keeps the unapplied Goal keeps its Undo", () => {
    const rebuilt = withGoalUndoRecordsFrom([edited], [codeBlock(true)]);

    expect(pendingGoalChangesOf(rebuilt)).toEqual([
      {
        label: "lookup_invoice",
        goal: "Download last month's invoice",
        previousGoal: "Download the latest statement",
      },
    ]);
  });

  test("a draft that applied the Goal, or changed its text, drops the Undo", () => {
    const applied = withGoalUndoRecordsFrom([edited], [codeBlock(false)]);
    const retyped = withGoalUndoRecordsFrom(
      [edited],
      [
        {
          ...codeBlock(true),
          data: { ...codeBlock(true).data, prompt: "Something else" },
        } as AppNode,
      ],
    );

    expect(pendingGoalChangesOf(applied)).toEqual([]);
    expect(pendingGoalChangesOf(retyped)[0]?.previousGoal).toBeNull();
  });
});

describe("blockRunErrors", () => {
  const otherBlock = {
    ...codeBlock(null),
    id: "cb2",
    data: { ...codeBlock(null).data, label: "send_report" },
  } as AppNode;

  test("a pending Goal on another block stops a single-block run", () => {
    const errors = blockRunErrors([codeBlock(true), otherBlock], "send_report");

    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("lookup_invoice");
  });

  test("with no pending Goal, only the run block's own errors stop it", () => {
    expect(
      blockRunErrors([codeBlock(false), otherBlock], "send_report"),
    ).toEqual([]);
  });
});

describe("terminate block error code round trip", () => {
  function terminateBlock(errorCode: string | null | undefined) {
    return {
      block_type: "terminate",
      label: "stop",
      continue_on_failure: false,
      next_loop_on_failure: false,
      model: null,
      ignore_workflow_system_prompt: false,
      reason: "No account matches {{ account_number }}",
      ...(errorCode === undefined ? {} : { error_code: errorCode }),
    } as unknown as WorkflowBlock;
  }

  function loadAndSaveTerminate(errorCode: string | null | undefined) {
    const { nodes, edges } = getElements(
      [terminateBlock(errorCode)],
      SETTINGS,
      true,
    );
    return getWorkflowBlocks(nodes, edges)[0] as unknown as Record<
      string,
      unknown
    >;
  }

  test.each([
    ["a 128-character code", "A".repeat(128), null],
    ["surrounding whitespace", `  ${"A".repeat(128)}  `, null],
    ["an empty code", "   ", null],
    ["a 129-character code", "A".repeat(129), "128 characters"],
    ["a control character", "BAD\u0000CODE", "Unicode category-C"],
    ["a Unicode format character", "BAD\u200dCODE", "Unicode category-C"],
    ["a literal encoded code", "AUTH%3cab", "ASCII letters"],
    ["a literal code with separators", "A_B.C:D-E", null],
    ["a templated code", "AUTH_{{ outcome_code }}", null],
    [
      "a long statement template",
      `{% if ${"true and ".repeat(20)}true %}ACCOUNT_LOCKED{% endif %}`,
      null,
    ],
    [
      "a statement-templated code",
      "{% if locked %}ACCOUNT_LOCKED{% else %}ACCOUNT_MISSING{% endif %}",
      null,
    ],
    ["a comment-templated code", "{# note #}ACCOUNT_LOCKED", null],
  ])("validates %s before save", (_, code, expectedMessage) => {
    const { nodes } = getElements([terminateBlock(code)], SETTINGS, true);
    const errors = getWorkflowErrors(nodes);

    expect(errors.filter((error) => error.includes("Error Code"))).toEqual(
      expectedMessage ? [expect.stringContaining(expectedMessage)] : [],
    );
  });

  test("offers a Terminate code in the retry rule picker", () => {
    const { nodes } = getElements(
      [terminateBlock("ACCOUNT_NOT_FOUND")],
      SETTINGS,
      true,
    );

    expect(collectKnownErrorCodes(nodes)).toContain("ACCOUNT_NOT_FOUND");
  });

  test("does not offer unrendered Terminate templates in the retry picker", () => {
    const { nodes } = getElements(
      [
        terminateBlock("ACCOUNT_NOT_FOUND"),
        { ...terminateBlock("{{ outcome_code }}"), label: "stop_2" },
        { ...terminateBlock("AUTH_{{ outcome_code }}"), label: "stop_3" },
        { ...terminateBlock("{% if ok %}READY{% endif %}"), label: "stop_4" },
      ],
      SETTINGS,
      true,
    );

    expect(collectKnownErrorCodes(nodes)).toEqual(["ACCOUNT_NOT_FOUND"]);
  });

  test.each([
    ["a stored code", "ACCOUNT_NOT_FOUND", "ACCOUNT_NOT_FOUND"],
    ["a templated code", "{{ outcome_code }}", "{{ outcome_code }}"],
    ["a definition saved before the field existed", undefined, null],
    ["a cleared code", "", null],
    ["a whitespace-only code", "   ", null],
  ])("%s loads and saves as %s", (_, stored, expected) => {
    expect(loadAndSaveTerminate(stored)).toMatchObject({
      block_type: "terminate",
      reason: "No account matches {{ account_number }}",
      error_code: expected,
    });
  });

  test("an export keeps the code and a definition without one exports null", () => {
    const source = {
      title: "Source",
      description: null,
      is_saved_task: false,
      status: null,
      run_with: "agent",
      workflow_definition: {
        parameters: [],
        blocks: [
          terminateBlock("ACCOUNT_NOT_FOUND"),
          { ...terminateBlock(undefined), label: "stop_2" },
        ],
      },
    } as unknown as WorkflowApiResponse;

    expect(
      convert(source).workflow_definition.blocks.map(
        (block) => (block as { error_code?: string | null }).error_code,
      ),
    ).toEqual(["ACCOUNT_NOT_FOUND", null]);
  });
});

function loadAndSave(
  stored: RunEngine | undefined,
  effectiveDefaultEngine: RunEngine | null | undefined,
) {
  const { nodes, edges } = getElements(
    [minimalTaskBlock(stored ? { engine: stored } : {})],
    SETTINGS,
    true,
    effectiveDefaultEngine,
  );
  const [saved] = getWorkflowBlocks(nodes, edges);
  return (saved as { engine?: RunEngine | null }).engine;
}

describe("block engine load and save", () => {
  test("a new block is Default and saves no engine", () => {
    const node = createNode({ id: "n1" }, "task", "My Task");

    expect(getWorkflowBlocks([node], [])[0]).toMatchObject({ engine: null });
  });

  test.each([
    ["unset", undefined, null, null],
    ["skyvern-1.0 on a routed workflow", RunEngine.SkyvernV1, null, null],
    [
      "skyvern-1.0 on a chosen-engine workflow",
      RunEngine.SkyvernV1,
      RunEngine.SkyvernV3,
      RunEngine.SkyvernV1,
    ],
    [
      "skyvern-1.0 when the workflow's default is unknown",
      RunEngine.SkyvernV1,
      undefined,
      RunEngine.SkyvernV1,
    ],
    ["skyvern-2.0", RunEngine.SkyvernV2, null, RunEngine.SkyvernV2],
    ["skyvern-3.0", RunEngine.SkyvernV3, null, RunEngine.SkyvernV3],
  ])("%s loads and saves as %s", (_, stored, effective, expected) => {
    expect(loadAndSave(stored, effective)).toBe(expected);
  });

  test("a copy drops a legacy skyvern-1.0 unless the source honours chosen engines; an export keeps it", () => {
    const source = {
      title: "Source",
      description: null,
      is_saved_task: false,
      status: null,
      run_with: "agent",
      workflow_definition: {
        parameters: [],
        blocks: [
          minimalTaskBlock({ engine: RunEngine.SkyvernV1 }),
          {
            ...minimalTaskBlock({ engine: RunEngine.SkyvernV3 }),
            label: "b2",
          },
        ],
      },
    } as unknown as WorkflowApiResponse;
    const engines = (request: ReturnType<typeof convert>) =>
      request.workflow_definition.blocks.map(
        (block) => (block as { engine?: RunEngine | null }).engine,
      );

    expect(engines(convert(source, { asNewWorkflow: true }))).toEqual([
      null,
      RunEngine.SkyvernV3,
    ]);
    expect(
      engines(
        convert(
          { ...source, effective_default_engine: RunEngine.SkyvernV3 },
          { asNewWorkflow: true },
        ),
      ),
    ).toEqual([RunEngine.SkyvernV1, RunEngine.SkyvernV3]);
    expect(engines(convert(source))).toEqual([
      RunEngine.SkyvernV1,
      RunEngine.SkyvernV3,
    ]);
  });
});
