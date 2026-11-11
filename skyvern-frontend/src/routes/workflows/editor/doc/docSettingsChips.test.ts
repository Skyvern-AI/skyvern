import { describe, expect, test } from "vitest";

import type { AppNode, WorkflowBlockNode } from "../nodes";
import { agentStepNodeDefaultData } from "../nodes/AgentStepNode/types";
import { emailInboxNodeDefaultData } from "../nodes/EmailInboxNode/types";
import { extractionNodeDefaultData } from "../nodes/ExtractionNode/types";
import { loopNodeDefaultData } from "../nodes/LoopNode/types";
import { navigationNodeDefaultData } from "../nodes/NavigationNode/types";
import { powerBIQueryNodeDefaultData } from "../nodes/PowerBIQueryNode/types";
import { docCustomSettings, yamlKey } from "./docSettingsChips";
import { docKeyFieldKeys, getDocProseFields } from "./docStepRegistry";
import { createNode } from "../workflowEditorUtils";
import { taskv2NodeDefaultData } from "../nodes/Taskv2Node/types";

describe("docCustomSettings", () => {
  const extraction = (data: Record<string, unknown>) =>
    ({
      id: "n",
      position: { x: 0, y: 0 },
      type: "extraction",
      data: {
        ...extractionNodeDefaultData,
        label: "find_prices",
        dataExtractionGoal: "Get the prices",
        ...data,
      },
    }) as AppNode;

  test("lists every saved setting that differs from a new step of the type, and nothing at its default", () => {
    expect(
      docCustomSettings(
        extraction({
          engine: "skyvern-2.0",
          maxStepsOverride: 12,
          maxRetries: 0,
          continueOnFailure: true,
          model: { model_name: "model-x" },
        }),
      )?.map((row) => [row.key, row.label, row.value]),
    ).toEqual([
      ["continue_on_failure", "Continue on failure", "Yes"],
      ["model", "Model", "model-x"],
      ["max_steps_per_run", "Max steps", "12"],
      ["engine", "Engine", "skyvern-2.0"],
    ]);
  });

  test("leaves out what the Doc shows elsewhere: the goal, the inputs and the output schema", () => {
    expect(
      docCustomSettings(
        extraction({
          dataExtractionGoal: "Something else entirely",
          parameterKeys: ["site"],
          dataSchema: '{"type": "object"}',
        }),
      ),
    ).toEqual([]);
  });

  const node = (type: string, data: Record<string, unknown>) =>
    ({ id: "n", position: { x: 0, y: 0 }, type, data }) as AppNode;
  const keys = (n: AppNode) => docCustomSettings(n)?.map((row) => row.key);

  test("an agent step and a Power BI query list their changed settings", () => {
    expect(
      keys(
        node("agentStep", {
          ...agentStepNodeDefaultData,
          label: "agent",
          maxTurns: 5,
        }),
      ),
    ).toEqual(["max_turns"]);
    expect(
      keys(
        node("powerBIQuery", {
          ...powerBIQueryNodeDefaultData,
          label: "bi",
          maxRows: 50,
        }),
      ),
    ).toEqual(["max_rows"]);
    expect(
      keys(node("agentStep", { ...agentStepNodeDefaultData, label: "a" })),
    ).toEqual([]);
    expect(
      keys(
        node("powerBIQuery", { ...powerBIQueryNodeDefaultData, label: "b" }),
      ),
    ).toEqual([]);
  });

  test("a step keeps the fields that pick its saved type, so its defaults stay defaults", () => {
    expect(
      keys(
        node("navigation", {
          ...navigationNodeDefaultData,
          label: "nav",
          engine: "skyvern-2.0",
          prompt: "Open the page",
        }),
      ),
    ).toEqual([]);
    expect(
      keys(
        node("loop", {
          ...loopNodeDefaultData,
          label: "again",
          loopKind: "while",
        }),
      ),
    ).toEqual([]);
  });

  test("a setting the user cleared is listed as Not set", () => {
    expect(
      docCustomSettings(
        node("emailInbox", {
          ...emailInboxNodeDefaultData,
          label: "inbox",
          folder: "",
        }),
      )?.map((row) => [row.key, row.value]),
    ).toEqual([["folder", "Not set"]]);
  });

  test("a while loop's expression is its main line, not a custom setting; the rest of its condition is", () => {
    const loop = (data: Record<string, unknown>) =>
      node("loop", {
        ...loopNodeDefaultData,
        label: "again",
        loopKind: "while",
        whileConditionExpression: "{{ more_pages }}",
        ...data,
      });
    expect(keys(loop({}))).toEqual([]);
    expect(
      keys(loop({ whileConditionDescription: "Until the last page" })),
    ).toEqual(["condition"]);
  });

  // Typed as a record so a new block type fails tsc here until it is listed.
  const BLOCK_TYPES: Record<WorkflowBlockNode["type"], true> = {
    task: true,
    taskv2: true,
    navigation: true,
    action: true,
    extraction: true,
    login: true,
    validation: true,
    human_interaction: true,
    wait: true,
    terminate: true,
    loop: true,
    conditional: true,
    codeBlock: true,
    textPrompt: true,
    sendEmail: true,
    emailInbox: true,
    fileDownload: true,
    fileUpload: true,
    download: true,
    upload: true,
    fileParser: true,
    pdfParser: true,
    pdfFill: true,
    splitPdf: true,
    dataExport: true,
    url: true,
    web_search: true,
    http_request: true,
    printPage: true,
    workflowTrigger: true,
    googleSheetsRead: true,
    googleSheetsWrite: true,
    agentStep: true,
    powerBIQuery: true,
  };
  const newStep = (
    type: WorkflowBlockNode["type"],
    data: Record<string, unknown> = {},
  ) =>
    node(type, {
      ...(type === "taskv2"
        ? taskv2NodeDefaultData
        : createNode({ id: "n" }, type, "").data),
      label: "step",
      ...data,
    });
  // Each variant picks a different serialization branch; the ones the saved type does not name list themselves.
  const variants: Array<
    [WorkflowBlockNode["type"], Record<string, unknown>, string[]]
  > = [
    ...(Object.keys(BLOCK_TYPES) as WorkflowBlockNode["type"][]).map(
      (type) =>
        [type, {}, []] as [
          WorkflowBlockNode["type"],
          Record<string, unknown>,
          string[],
        ],
    ),
    ["navigation", { engine: "skyvern-2.0" }, []],
    ["loop", { loopKind: "while" }, []],
    ["sendEmail", { transport: "gmail" }, ["transport"]],
    ...["s3", "azure", "google_drive", "sftp"].map(
      (target) =>
        ["fileDownload", { downloadTarget: target }, ["download_target"]] as [
          WorkflowBlockNode["type"],
          Record<string, unknown>,
          string[],
        ],
    ),
  ];

  test.each(variants)(
    "a new %s step %j lists nothing custom but its branch",
    (type, data, expected) => {
      expect(keys(newStep(type, data))).toEqual(expected);
    },
  );

  test("a Gmail step with a cleared field lists that field and its transport", () => {
    expect(
      docCustomSettings(
        newStep("sendEmail", { transport: "gmail", bodyFormat: "" }),
      )?.map((row) => [row.key, row.value]),
    ).toEqual([
      ["transport", "gmail"],
      ["body_format", "Not set"],
    ]);
  });

  test.each([
    ["fileUpload", { storageType: "s3", awsSecretAccessKey: "raw-secret-1" }],
    [
      "fileUpload",
      { storageType: "azure", azureStorageAccountKey: "raw-secret-2" },
    ],
    [
      "fileUpload",
      {
        storageType: "sftp",
        sftpPassword: "raw-secret-3",
        sftpPrivateKey: "raw-secret-4",
      },
    ],
    ["sendEmail", { customSmtpPassword: "raw-secret-5" }],
  ] as const)(
    "a %s step's secret settings read as hidden, never as their value: %j",
    (type, data) => {
      const rows = docCustomSettings(newStep(type, data)) ?? [];
      const secrets = Object.keys(data)
        .filter((key) => key !== "storageType")
        .map(yamlKey);
      expect(
        secrets.map((key) => rows.find((row) => row.key === key)?.value),
      ).toEqual(secrets.map(() => "•••••••• (hidden)"));
      expect(rows.filter((row) => row.value.includes("raw-secret"))).toEqual(
        [],
      );
    },
  );

  test("a trigger without a title lists its target, since the Agent row cannot name it", () => {
    const target = { workflowPermanentId: "wpid_target" };
    expect(keys(newStep("workflowTrigger", target))).toEqual([
      "workflow_permanent_id",
    ]);
    expect(
      keys(
        newStep("workflowTrigger", { ...target, workflowTitle: "Pay bills" }),
      ),
    ).toEqual([]);
  });

  test("no field the Doc shows in its own place is also listed under Custom settings", () => {
    for (const type of Object.keys(
      BLOCK_TYPES,
    ) as WorkflowBlockNode["type"][]) {
      const base = newStep(type);
      const shownKeys = [
        ...getDocProseFields(base).map((f) => f.key),
        ...docKeyFieldKeys(type),
        "notes",
        "trailingNotes",
        "description",
        ...(type === "workflowTrigger" ? ["workflowPermanentId"] : []),
      ];
      const set = (value: unknown) =>
        Array.isArray(value)
          ? ["someone@example.com"]
          : typeof value === "number"
            ? 7
            : "{{ set_value }}";
      const data = base.data as Record<string, unknown>;
      const filled = newStep(
        type,
        Object.fromEntries(shownKeys.map((key) => [key, set(data[key])])),
      );
      const listed = keys(filled) ?? [];
      expect([
        type,
        listed.filter((key) => shownKeys.map(yamlKey).includes(key)),
      ]).toEqual([type, []]);
    }
  });
});
