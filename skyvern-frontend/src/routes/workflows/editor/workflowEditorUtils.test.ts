import { QueryClient } from "@tanstack/react-query";
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
  workflowEffectiveDefaultEngine,
} from "./workflowEditorUtils";
import { agentStepToolsFromSelection } from "./nodes/AgentStepNode/types";
import { collectKnownErrorCodes } from "./nodes/StartNode/retryPolicyUtils";
import { terminateNodeDefaultData } from "./nodes/TerminateNode/types";

function minimalTaskBlock(
  overrides: { engine?: RunEngine; engine_pinned?: boolean } = {},
) {
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

describe("send email attachments", () => {
  test.each([
    ["an empty field", "", []],
    ["a blank field", "   ", []],
    ["stray commas", "a.pdf, , b.pdf,", ["a.pdf", "b.pdf"]],
  ])("%s saves only the real entries", (_, fileAttachments, expected) => {
    const node = createNode({ id: "n1" }, "sendEmail", "Notify");
    const edited = { ...node, data: { ...node.data, fileAttachments } };

    expect(getWorkflowBlocks([edited as AppNode], [])[0]).toMatchObject({
      file_attachments: expected,
    });
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

describe("agent block load and save", () => {
  const dataSchema = {
    type: "object",
    properties: { plan_name: { type: "string" } },
    required: ["plan_name"],
  };
  const agentStepBlock = {
    block_type: "agent_step",
    label: "find_plan",
    continue_on_failure: false,
    next_loop_on_failure: false,
    model: null,
    ignore_workflow_system_prompt: false,
    prompt: "Find the plan for {{ account_number }}",
    data_schema: dataSchema,
    max_turns: 40,
    parameters: [{ key: "account_number" }],
  } as unknown as WorkflowBlock;
  const savedFields = {
    label: "find_plan",
    block_type: "agent_step",
    prompt: "Find the plan for {{ account_number }}",
    data_schema: dataSchema,
    max_turns: 40,
    parameter_keys: ["account_number"],
  };

  test.each([
    ["a tools list", ["http_request", "web_search"]],
    ["no tools list", null],
  ])(
    "an existing block with %s saves unchanged from the canvas and from an export",
    (_, tools) => {
      const block = { ...agentStepBlock, tools } as WorkflowBlock;
      const saved = { ...savedFields, tools };
      const { nodes, edges } = getElements([block], SETTINGS, true);

      expect(getWorkflowErrors(nodes)).toEqual([]);
      expect(getWorkflowBlocks(nodes, edges)[0]).toMatchObject(saved);
      const source = {
        title: "Source",
        description: null,
        is_saved_task: false,
        status: null,
        run_with: "agent",
        workflow_definition: { parameters: [], blocks: [block] },
      } as unknown as WorkflowApiResponse;
      expect(convert(source).workflow_definition.blocks[0]).toMatchObject(
        saved,
      );
    },
  );

  test("checking every tool saves no list, and checking some saves those", () => {
    const choosable = ["code", "http_request", "web_search"];

    expect(
      agentStepToolsFromSelection(
        ["web_search", "code", "http_request"],
        choosable,
      ),
    ).toBeNull();
    expect(agentStepToolsFromSelection(["http_request"], choosable)).toEqual([
      "http_request",
    ]);
  });

  test("a new agent step blocks the save until a tool is chosen", () => {
    const node = createNode({ id: "n1" }, "agentStep", "find_plan");

    expect(getWorkflowErrors([node])).toContain(
      "find_plan: Choose at least one tool.",
    );
  });

  test.each([
    ["a half-typed schema", { dataSchema: '{"type": "obj' }, "not valid JSON"],
    [
      "a non-object schema",
      { dataSchema: '{"type": "array"}' },
      '"type": "object"',
    ],
    ["an empty prompt", { prompt: "  " }, "Prompt is required."],
  ])(
    "%s does not throw on serialize and blocks the save",
    (_, draft, message) => {
      const { nodes, edges } = getElements([agentStepBlock], SETTINGS, true);
      const edited = nodes.map((node) =>
        node.type === "agentStep"
          ? { ...node, data: { ...node.data, ...draft } }
          : node,
      ) as Array<AppNode>;

      expect(() => getWorkflowBlocks(edited, edges)).not.toThrow();
      expect(getWorkflowErrors(edited)).toEqual([
        expect.stringContaining(message),
      ]);
    },
  );
});

function loadAndSave(
  stored: { engine?: RunEngine; engine_pinned?: boolean },
  effectiveDefaultEngine: RunEngine | null | undefined,
) {
  const { nodes, edges } = getElements(
    [minimalTaskBlock(stored)],
    SETTINGS,
    true,
    effectiveDefaultEngine,
  );
  const [saved] = getWorkflowBlocks(nodes, edges);
  const { engine, engine_pinned } = saved as {
    engine?: RunEngine | null;
    engine_pinned?: boolean;
  };
  return { engine, engine_pinned };
}

describe("block engine load and save", () => {
  test("a new block is Default and saves no engine", () => {
    const node = createNode({ id: "n1" }, "task", "My Task");

    expect(getWorkflowBlocks([node], [])[0]).toMatchObject({ engine: null });
  });

  const v1 = RunEngine.SkyvernV1;
  test.each([
    ["unset", {}, null, { engine: null }],
    [
      "an unmarked skyvern-1.0 on a routed workflow",
      { engine: v1 },
      null,
      { engine: null },
    ],
    [
      "a marked skyvern-1.0 on a routed workflow",
      { engine: v1, engine_pinned: true },
      null,
      { engine: v1, engine_pinned: true },
    ],
    [
      "skyvern-1.0 on a chosen-engine workflow",
      { engine: v1 },
      RunEngine.SkyvernV3,
      { engine: v1 },
    ],
    [
      "skyvern-1.0 when the workflow's default is unknown",
      { engine: v1 },
      undefined,
      { engine: v1 },
    ],
    [
      "skyvern-2.0",
      { engine: RunEngine.SkyvernV2 },
      null,
      { engine: RunEngine.SkyvernV2 },
    ],
    [
      "skyvern-3.0",
      { engine: RunEngine.SkyvernV3 },
      null,
      { engine: RunEngine.SkyvernV3 },
    ],
  ])(
    "%s round-trips through load and save",
    (_, stored, effective, expected) => {
      expect(loadAndSave(stored, effective)).toEqual(expected);
    },
  );

  test("a save response, which omits the workflow's default engine, shows an unmarked skyvern-1.0 as Default", () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(["workflow", "wpid_old"], {
      workflow_permanent_id: "wpid_old",
      effective_default_engine: null,
    });
    const saveResponse = { workflow_permanent_id: "wpid_old" };

    expect(
      loadAndSave(
        { engine: v1 },
        workflowEffectiveDefaultEngine(saveResponse, queryClient),
      ),
    ).toEqual({ engine: null });
    expect(
      workflowEffectiveDefaultEngine(
        { ...saveResponse, effective_default_engine: RunEngine.SkyvernV3 },
        queryClient,
      ),
    ).toBe(RunEngine.SkyvernV3);
  });

  test("a copy drops a legacy skyvern-1.0 unless the source honours chosen engines or a person marked it; an export keeps it", () => {
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
          {
            ...minimalTaskBlock({
              engine: RunEngine.SkyvernV1,
              engine_pinned: true,
            }),
            label: "b3",
          },
        ],
      },
    } as unknown as WorkflowApiResponse;
    const engines = (request: ReturnType<typeof convert>) =>
      request.workflow_definition.blocks.map(
        (block) => (block as { engine?: RunEngine | null }).engine,
      );

    const copy = convert(source, { asNewWorkflow: true });
    expect(engines(copy)).toEqual([
      null,
      RunEngine.SkyvernV3,
      RunEngine.SkyvernV1,
    ]);
    expect(copy.workflow_definition.blocks[2]).toMatchObject({
      engine_pinned: true,
    });
    expect(copy.workflow_definition.blocks[0]).not.toHaveProperty(
      "engine_pinned",
    );
    expect(
      engines(
        convert(
          { ...source, effective_default_engine: RunEngine.SkyvernV3 },
          { asNewWorkflow: true },
        ),
      ),
    ).toEqual([RunEngine.SkyvernV1, RunEngine.SkyvernV3, RunEngine.SkyvernV1]);
    expect(engines(convert(source))).toEqual([
      RunEngine.SkyvernV1,
      RunEngine.SkyvernV3,
      RunEngine.SkyvernV1,
    ]);
  });
});

describe("block model override on save", () => {
  test("a new navigation block saves no model override", () => {
    const node = createNode({ id: "n1" }, "navigation", "Go to page");

    expect(getWorkflowBlocks([node], [])[0]).toMatchObject({ model: null });
  });

  test.each([
    ["an empty model name is dropped", { model_name: "" }, null],
    [
      "a chosen model is kept",
      { model_name: "gemini-3-flash" },
      { model_name: "gemini-3-flash" },
    ],
  ])("%s", (_name, stored, expected) => {
    const { nodes, edges } = getElements(
      [{ ...minimalTaskBlock(), model: stored } as WorkflowBlock],
      SETTINGS,
      true,
    );

    expect(getWorkflowBlocks(nodes, edges)[0]).toMatchObject({
      model: expected,
    });
  });
});

describe("FileDownloadBlock SFTP source round trip", () => {
  test("preserves source path and shared credentials for website downloads", () => {
    const sourceBlock = {
      block_type: "file_download",
      label: "read_report",
      continue_on_failure: false,
      next_loop_on_failure: false,
      url: null,
      title: "Read report",
      navigation_goal: "Read the report",
      error_code_mapping: null,
      parameters: [],
      download_target: "website",
      path: "{{ workflow_run_id }}",
      prompt: null,
      sftp_source_path: "/reports/report.csv",
      sftp_host: "sftp.example.test",
      sftp_port: 2222,
      sftp_username: "source_user",
      sftp_password: "source_password",
      sftp_private_key: "source_private_key",
      sftp_private_key_passphrase: "source_passphrase",
      sftp_host_key: "ssh-ed25519 AAAA",
    } as unknown as WorkflowBlock;

    const { nodes, edges } = getElements([sourceBlock], SETTINGS, true);
    const fileDownloadNode = nodes.find((node) => node.type === "fileDownload");
    expect(fileDownloadNode?.data).toMatchObject({
      sftpSourcePath: "/reports/report.csv",
      sftpHost: "sftp.example.test",
      sftpPort: "2222",
      sftpUsername: "source_user",
      sftpPassword: "source_password",
      sftpPrivateKey: "source_private_key",
      sftpPrivateKeyPassphrase: "source_passphrase",
      sftpHostKey: "ssh-ed25519 AAAA",
    });

    const savedFromNodes = getWorkflowBlocks(nodes, edges)[0];
    expect(savedFromNodes).toMatchObject({
      block_type: "file_download",
      sftp_source_path: "/reports/report.csv",
      sftp_host: "sftp.example.test",
      sftp_port: 2222,
      sftp_username: "source_user",
      sftp_password: "source_password",
      sftp_private_key: "source_private_key",
      sftp_private_key_passphrase: "source_passphrase",
      sftp_host_key: "ssh-ed25519 AAAA",
    });
    expect(savedFromNodes).not.toHaveProperty("sftp_remote_path");

    const exportedBlock = convert({
      workflow_definition: {
        version: 1,
        parameters: [],
        blocks: [sourceBlock],
      },
    } as unknown as WorkflowApiResponse).workflow_definition.blocks[0];
    expect(exportedBlock).toMatchObject({
      sftp_source_path: "/reports/report.csv",
      sftp_host: "sftp.example.test",
      sftp_port: 2222,
      sftp_username: "source_user",
      sftp_password: "source_password",
      sftp_private_key: "source_private_key",
      sftp_private_key_passphrase: "source_passphrase",
      sftp_host_key: "ssh-ed25519 AAAA",
    });
    expect(exportedBlock).not.toHaveProperty("sftp_remote_path");
  });
});

describe("block note metadata round trip", () => {
  test("preserves notes and explicit clears through editor serialization", () => {
    const noted = {
      ...minimalTaskBlock(),
      notes: "Read the saved plan first.",
      trailing_notes: "Stop after confirming the result.",
      description: "Only for the workflow author.",
    } as WorkflowBlock;
    const oldVersion = {
      ...minimalTaskBlock(),
      label: "old_version",
    } as WorkflowBlock;
    const { nodes, edges } = getElements([noted, oldVersion], SETTINGS, false);
    const saved = getWorkflowBlocks(nodes, edges);

    expect(saved.find((block) => block.label === "b1")).toMatchObject({
      notes: "Read the saved plan first.",
      trailing_notes: "Stop after confirming the result.",
      description: "Only for the workflow author.",
    });
    const savedOldVersion = saved.find(
      (block) => block.label === "old_version",
    );
    expect(savedOldVersion).toBeDefined();
    for (const key of ["notes", "trailing_notes", "description"] as const) {
      expect(savedOldVersion).not.toHaveProperty(key);
    }
  });

  test.each([
    [
      "for_loop",
      {
        block_type: "for_loop",
        loop_over: { key: "items" },
        loop_variable_reference: "item",
        complete_if_empty: false,
        data_schema: null,
      },
    ],
    [
      "while_loop",
      {
        block_type: "while_loop",
        condition: {
          criteria_type: "jinja2_template",
          expression: "{{ true }}",
          description: null,
        },
      },
    ],
  ])("preserves metadata on %s blocks", (_type, typeFields) => {
    const loop = {
      label: "loop",
      continue_on_failure: false,
      next_loop_on_failure: false,
      model: null,
      ignore_workflow_system_prompt: false,
      loop_blocks: [],
      notes: "Use the saved notes.",
      trailing_notes: "Finish after this loop.",
      description: "Loop description.",
      ...typeFields,
    } as unknown as WorkflowBlock;
    const { nodes, edges } = getElements([loop], SETTINGS, false);

    expect(getWorkflowBlocks(nodes, edges)[0]).toMatchObject({
      notes: "Use the saved notes.",
      trailing_notes: "Finish after this loop.",
      description: "Loop description.",
    });
  });

  test("preserves conditional note metadata through save and copy/export serialization", () => {
    const loopChild = {
      ...minimalTaskBlock(),
      label: "loop_child",
      notes: "Read the loop value.",
      trailing_notes: "Finish the loop step.",
      description: "Loop child description.",
    } as WorkflowBlock;
    const conditional = {
      block_type: "conditional",
      label: "branch",
      continue_on_failure: false,
      next_block_label: null,
      branch_conditions: [],
      notes: "Read the plan before evaluating this branch.",
      trailing_notes: "Confirm the selected path.",
      description: "Only for the workflow author.",
      model: null,
      ignore_workflow_system_prompt: false,
    } as unknown as WorkflowBlock;
    const loop = {
      block_type: "for_loop",
      label: "loop",
      continue_on_failure: false,
      next_block_label: null,
      loop_over: { key: "items" },
      loop_variable_reference: null,
      complete_if_empty: false,
      data_schema: null,
      loop_blocks: [loopChild],
      model: null,
      ignore_workflow_system_prompt: false,
    } as unknown as WorkflowBlock;
    const { nodes, edges } = getElements([conditional], SETTINGS, false);
    const copiedBlocks = convert({
      workflow_definition: {
        version: 1,
        parameters: [],
        blocks: [conditional, loop],
      },
    } as unknown as WorkflowApiResponse).workflow_definition.blocks;

    expect(getWorkflowBlocks(nodes, edges)[0]).toMatchObject({
      notes: "Read the plan before evaluating this branch.",
      trailing_notes: "Confirm the selected path.",
      description: "Only for the workflow author.",
    });
    const [copiedConditional, copiedLoop] = copiedBlocks;
    expect(copiedConditional).toMatchObject({
      notes: "Read the plan before evaluating this branch.",
      trailing_notes: "Confirm the selected path.",
      description: "Only for the workflow author.",
    });
    expect(copiedLoop?.block_type).toBe("for_loop");
    if (copiedLoop?.block_type === "for_loop") {
      expect(copiedLoop.loop_blocks[0]).toMatchObject({
        notes: "Read the loop value.",
        trailing_notes: "Finish the loop step.",
        description: "Loop child description.",
      });
    }
  });

  test("clears absent conditional note metadata when serializing a restored version", () => {
    const conditional = {
      block_type: "conditional",
      label: "branch",
      continue_on_failure: false,
      next_block_label: null,
      branch_conditions: [],
      model: null,
      ignore_workflow_system_prompt: false,
    } as unknown as WorkflowBlock;
    const { nodes, edges } = getElements([conditional], SETTINGS, false);

    const saved = getWorkflowBlocks(nodes, edges)[0];
    for (const key of ["notes", "trailing_notes", "description"] as const) {
      expect(saved).not.toHaveProperty(key);
    }
  });
});
