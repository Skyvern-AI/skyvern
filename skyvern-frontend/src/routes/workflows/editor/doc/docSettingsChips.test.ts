import { describe, expect, test } from "vitest";

import type { AppNode, WorkflowBlockNode } from "../nodes";
import { actionNodeDefaultData } from "../nodes/ActionNode/types";
import { agentStepNodeDefaultData } from "../nodes/AgentStepNode/types";
import { codeBlockNodeDefaultData } from "../nodes/CodeBlockNode/types";
import { emailInboxNodeDefaultData } from "../nodes/EmailInboxNode/types";
import { extractionNodeDefaultData } from "../nodes/ExtractionNode/types";
import { fileDownloadNodeDefaultData } from "../nodes/FileDownloadNode/types";
import { loginNodeDefaultData } from "../nodes/LoginNode/types";
import { loopNodeDefaultData } from "../nodes/LoopNode/types";
import { navigationNodeDefaultData } from "../nodes/NavigationNode/types";
import { powerBIQueryNodeDefaultData } from "../nodes/PowerBIQueryNode/types";
import { sendEmailNodeDefaultData } from "../nodes/SendEmailNode/types";
import { createNode, getWorkflowBlocks } from "../workflowEditorUtils";
import { taskNodeDefaultData } from "../nodes/TaskNode/types";
import { taskv2NodeDefaultData } from "../nodes/Taskv2Node/types";
import { workflowTriggerNodeDefaultData } from "../nodes/WorkflowTriggerNode/types";
import { httpRequestNodeDefaultData } from "../nodes/HttpRequestNode/types";
import { urlNodeDefaultData } from "../nodes/URLNode/types";
import { getDocProseFields } from "./docStepRegistry";
import { DOC_FIELD_MAP } from "./docFieldMap";
import {
  docCustomSettings,
  docKeySettings,
  docLoginCredentialKeys,
  docSettingRowsSplit,
  yamlKey,
} from "./docSettingsChips";
import type { ParametersState } from "../types";

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

  test("lists every saved setting that differs from a new step of the type, except its key settings, and nothing at its default", () => {
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

  test("a trigger's target is never a custom setting: its Agent row names it", () => {
    const target = { workflowPermanentId: "wpid_target" };
    expect(keys(newStep("workflowTrigger", target))).toEqual([]);
    expect(
      keys(
        newStep("workflowTrigger", { ...target, workflowTitle: "Pay bills" }),
      ),
    ).toEqual([]);
  });

  test("no field the Doc shows in its own place is also listed under Custom settings", () => {
    const camel = (key: string) =>
      key.replace(/_([a-z0-9])/g, (_, c: string) => c.toUpperCase());
    const set = (value: unknown) =>
      Array.isArray(value)
        ? ["someone@example.com"]
        : typeof value === "number"
          ? 7
          : typeof value === "boolean"
            ? !value
            : "{{ set_value }}";
    for (const type of Object.keys(
      BLOCK_TYPES,
    ) as WorkflowBlockNode["type"][]) {
      const base = newStep(type);
      const data = base.data as Record<string, unknown>;
      const [saved] = getWorkflowBlocks([base], []) as unknown as Record<
        string,
        unknown
      >[];
      const specKeys = (
        DOC_FIELD_MAP[saved!.block_type as keyof typeof DOC_FIELD_MAP] ?? []
      ).flatMap((spec) => spec.keys);
      const dataKeys = [
        ...getDocProseFields(base).map((f) => f.key),
        ...specKeys.map(camel).filter((key) => key in data),
        "notes",
        "trailingNotes",
        "description",
        ...(type === "workflowTrigger" ? ["workflowPermanentId"] : []),
      ];
      const filled = newStep(
        type,
        Object.fromEntries(dataKeys.map((key) => [key, set(data[key])])),
      );
      const shown = new Set([...dataKeys.map(yamlKey), ...specKeys]);
      expect([
        type,
        (keys(filled) ?? []).filter((key) => shown.has(key)),
      ]).toEqual([type, []]);
    }
  });
});

describe("docKeySettings", () => {
  const node = (type: string, data: Record<string, unknown>) =>
    ({
      id: "n",
      position: { x: 0, y: 0 },
      type,
      data: { ...data, label: "step_one" },
    }) as AppNode;
  const rows = (n: AppNode) =>
    docKeySettings(n, new Set(["site_login"])).map((row) => [
      row.label,
      row.value,
    ]);

  test("a login shows its credential and 2FA first, with the empty text for a field that has no value", () => {
    expect(
      rows(
        node("login", {
          ...loginNodeDefaultData,
          // Only the first entry is the credential slot; later entries are inputs the login reads.
          parameterKeys: ["site_login", "username"],
          totpIdentifier: "codes@example.com",
          url: "https://example.com/login",
        }),
      ),
    ).toEqual([
      ["Credential", "site_login"],
      ["2FA", "codes@example.com"],
      ["Error codes", "Not set"],
      ["URL", "https://example.com/login"],
    ]);
    // The selected credential can carry the 2FA identifier at run time, so an empty field is not "Off".
    expect(rows(node("login", loginNodeDefaultData))[1]).toEqual([
      "2FA",
      "Not set",
    ]);
  });

  test("a row the saved block does not use is left out: Sender on Gmail, Download timeout on an SFTP download", () => {
    const labels = (transport: string) =>
      rows(node("sendEmail", { ...sendEmailNodeDefaultData, transport })).map(
        ([label]) => label,
      );
    expect(labels("gmail")).not.toContain("Sender");
    expect(labels("smtp")).toContain("Sender");
    const download = (data: Record<string, unknown>) =>
      rows(
        node("fileDownload", { ...fileDownloadNodeDefaultData, ...data }),
      ).map(([label]) => label);
    expect(
      download({ downloadTarget: "sftp", sftpSourcePath: "/in/report.pdf" }),
    ).not.toContain("Download timeout");
    expect(download({})).toContain("Download timeout");
  });

  test("a key a hiddenWhen rule leaves out shows under Custom settings when it differs from a new step's", () => {
    const customKeys = (n: AppNode) =>
      docCustomSettings(n)?.map((row) => row.key);
    for (const method of ["GET", "HEAD", "DELETE"]) {
      const request = node("http_request", {
        ...httpRequestNodeDefaultData,
        method,
        body: '{"id": 1}',
      });
      expect(rows(request).map(([label]) => label)).not.toContain("Body");
      expect(customKeys(request)).toContain("body");
    }
    const sftp = node("fileDownload", {
      ...fileDownloadNodeDefaultData,
      downloadTarget: "sftp",
      sftpSourcePath: "/in/report.pdf",
      downloadTimeout: 90,
    });
    expect(rows(sftp).map(([label]) => label)).not.toContain(
      "Download timeout",
    );
    expect(customKeys(sftp)).toContain("download_timeout");
  });

  test("a value that formats to blank text reads as unset: no SMTP recipients, a blank subject", () => {
    const email = docKeySettings(
      node("sendEmail", {
        ...sendEmailNodeDefaultData,
        transport: "smtp",
        recipients: "",
        subject: "   ",
      }),
      new Set(),
    );
    expect(email.find((row) => row.key === "recipients")).toMatchObject({
      value: "Not set",
      unset: true,
      changed: false,
    });
    expect(email.find((row) => row.key === "subject")).toMatchObject({
      value: "Not set",
      unset: true,
    });
  });

  test("an HTTP request shows its Body only for a method that sends one", () => {
    const labels = (method: string) =>
      rows(
        node("http_request", {
          ...httpRequestNodeDefaultData,
          method,
          body: "{}",
        }),
      ).map(([label]) => label);
    expect(labels("GET")).not.toContain("Body");
    expect(labels("POST")).toContain("Body");
  });

  test("an HTTP request shows its Method; a Go to URL step has no key settings", () => {
    expect(
      rows(
        node("http_request", { ...httpRequestNodeDefaultData, method: "POST" }),
      ).find(([label]) => label === "Method"),
    ).toEqual(["Method", "POST"]);
    expect(
      rows(node("url", { ...urlNodeDefaultData, url: "https://example.com" })),
    ).toEqual([]);
  });

  test("a trigger's Agent row shows its title, or its target when the title is not known", () => {
    const trigger = (workflowTitle: string) =>
      rows(
        node("workflowTrigger", {
          ...workflowTriggerNodeDefaultData,
          workflowPermanentId: "wpid_target",
          workflowTitle,
        }),
      );
    expect(trigger("Pay bills")).toEqual([["Agent", "Pay bills"]]);
    expect(trigger("")).toEqual([["Agent", "wpid_target"]]);
  });

  test("a login names only its credential parameter, by the Login panel's credential rules", () => {
    const parameters = [
      {
        key: "site_login",
        parameterType: "credential",
        credentialId: "cred_1",
      },
      {
        key: "account",
        parameterType: "context",
        sourceParameterKey: "site_login",
      },
    ] as unknown as ParametersState;
    const credential = (keys: string[]) =>
      docKeySettings(
        node("login", { ...loginNodeDefaultData, parameterKeys: keys }),
        docLoginCredentialKeys(parameters),
      ).find((row) => row.label === "Credential")?.value;
    expect(credential(["site_login", "account"])).toBe("site_login");
    expect(credential(["account"])).toBeUndefined();
    expect(credential([])).toBe("Not set");
  });

  test("a login's Credential row reads only the first entry, as the Login panel does", () => {
    const credential = (keys: string[]) =>
      docKeySettings(
        node("login", { ...loginNodeDefaultData, parameterKeys: keys }),
        new Set(["site_login", "backup_login"]),
      ).find((row) => row.label === "Credential")?.value;
    expect(credential(["site_login", "backup_login"])).toBe("site_login");
    expect(credential(["username", "site_login"])).toBeUndefined();
  });

  test("a set value that reads as zero or No is shown as set, not as the empty text", () => {
    const extraction = docKeySettings(
      node("extraction", { ...extractionNodeDefaultData, maxRetries: 0 }),
      new Set(),
    ).find((row) => row.label === "Max retries");
    expect([extraction?.value, extraction?.unset]).toEqual(["0", false]);
    const code = docKeySettings(
      node("codeBlock", {
        ...codeBlockNodeDefaultData,
        continueOnFailure: false,
      }),
      new Set(),
    ).find((row) => row.label === "Continue on failure");
    expect([code?.value, code?.unset]).toEqual(["No", false]);
  });

  test("a login whose inputs include no credential input shows no Credential row, since the Doc cannot tell which is its credential", () => {
    expect(
      rows(
        node("login", { ...loginNodeDefaultData, parameterKeys: ["username"] }),
      ).map(([label]) => label),
    ).toEqual(["2FA", "Error codes", "URL"]);
  });

  test("a navigation on the 2.0 engine saves as task_v2 and shows no Navigation key settings", () => {
    expect(
      rows(
        node("navigation", {
          ...navigationNodeDefaultData,
          engine: "skyvern-2.0",
          prompt: "Open the page",
        }),
      ),
    ).toEqual([]);
  });

  test("a file download leads with its file name; a navigation lists its URL", () => {
    expect(
      rows(
        node("fileDownload", {
          ...fileDownloadNodeDefaultData,
          downloadSuffix: "invoice.pdf",
        }),
      )[0],
    ).toEqual(["File name", "invoice.pdf"]);
    expect(
      rows(node("navigation", navigationNodeDefaultData)).map(([l]) => l),
    ).toEqual(["Engine", "Max steps", "Error codes", "URL"]);
  });
});

describe("docSettingRowsSplit", () => {
  const node = (type: string, data: Record<string, unknown>) =>
    ({
      id: "n",
      position: { x: 0, y: 0 },
      type,
      data: { ...data, label: "step_one" },
    }) as AppNode;
  // What the summary line shows: [label, value, dashed] per field.
  const split = (n: AppNode, editable: boolean, opensFirstPage: boolean) =>
    docSettingRowsSplit(
      docKeySettings(n, new Set(["site_login"])),
      editable,
      opensFirstPage,
    );
  const line = (n: AppNode, editable = true, opensFirstPage = false) =>
    split(n, editable, opensFirstPage).line.map((row) => [
      row.label,
      row.value,
      Boolean(row.unset),
    ]);
  const detailsLabels = (n: AppNode, opensFirstPage: boolean) =>
    split(n, true, opensFirstPage).details.map((row) => row.label);
  const login = (data: Record<string, unknown>) =>
    node("login", { ...loginNodeDefaultData, ...data });

  test.each([
    [
      "a filled login",
      login({ parameterKeys: ["site_login"], url: "https://example.com/a" }),
      [
        ["Credential", "site_login", false],
        ["URL", "https://example.com/a", false],
      ],
    ],
    [
      "a new login gets a dashed chip for each",
      login({}),
      [
        ["Credential", "Not set", true],
        ["URL", "Not set", true],
      ],
    ],
    [
      "a login whose first input is not a credential has no credential row, so no chip",
      login({ parameterKeys: ["plain_input"], url: "" }),
      [["URL", "Not set", true]],
    ],
  ])("%s", (_case, n, expected) => {
    expect(line(n)).toEqual(expected);
  });

  test("a read-only Doc shows set fields only; an unset one stays in Details", () => {
    expect(line(login({ url: "https://example.com/a" }), false)).toEqual([
      ["URL", "https://example.com/a", false],
    ]);
  });

  // The backend fails each of these with MissingStarterUrl when it opens the run's first page with no URL.
  test.each([
    ["navigation", navigationNodeDefaultData],
    ["task", taskNodeDefaultData],
    ["action", actionNodeDefaultData],
    ["extraction", extractionNodeDefaultData],
    ["fileDownload", fileDownloadNodeDefaultData],
  ])(
    "a %s step's URL: a dashed chip on the step that opens the first page, nowhere on a later one, on the line when set",
    (type, data) => {
      const step = node(type, data);
      expect(line(step, true, true)).toContainEqual(["URL", "Not set", true]);
      expect(line(step, true, false)).not.toContainEqual(
        expect.arrayContaining(["URL"]),
      );
      expect(detailsLabels(step, false)).not.toContain("URL");
      expect(
        line(node(type, { ...data, url: "https://example.com/a" })),
      ).toContainEqual(["URL", "https://example.com/a", false]);
    },
  );

  test("Send Email always asks for its recipients; an unset subject stays in Details", () => {
    expect(line(node("sendEmail", sendEmailNodeDefaultData))).toEqual([
      ["Recipients", "Not set", true],
    ]);
  });
});

describe("DOC_FIELD_MAP", () => {
  test("lists only fields the editor saves for that block type", () => {
    const savedKeys = new Map<string, Set<string>>();
    // A field shown as one of the step's lines never renders as a key setting.
    const lineKeys = new Map<string, Set<string>>();
    for (const type of Object.keys(BLOCK_TYPES))
      // The variants that save as another block type or branch, and one with the fields saved only when set.
      for (const variant of [
        {},
        { loopKind: "while" },
        { engine: "skyvern-2.0" },
        { maxRetries: 1 },
        { transport: "gmail" },
        { downloadTarget: "sftp" },
        { downloadTarget: "s3" },
      ]) {
        const data =
          type === "taskv2"
            ? taskv2NodeDefaultData
            : createNode({ id: "n" }, type as WorkflowBlockNode["type"], "")
                .data;
        const [block] = getWorkflowBlocks(
          [
            {
              id: "n",
              position: { x: 0, y: 0 },
              type,
              data: { ...data, ...variant, label: "b" },
            } as AppNode,
          ],
          [],
        ) as unknown as Record<string, unknown>[];
        const keys = savedKeys.get(String(block!.block_type)) ?? new Set();
        Object.keys(block!).forEach((key) => keys.add(key));
        savedKeys.set(String(block!.block_type), keys);
        const lines = lineKeys.get(String(block!.block_type)) ?? new Set();
        getDocProseFields({
          id: "n",
          position: { x: 0, y: 0 },
          type,
          data: { ...data, ...variant },
        } as AppNode).forEach((f) => lines.add(yamlKey(f.key)));
        lineKeys.set(String(block!.block_type), lines);
      }
    const unsaved = Object.entries(DOC_FIELD_MAP).flatMap(([type, specs]) =>
      specs!.flatMap((spec) =>
        spec.keys
          .filter((key) => !savedKeys.get(type)?.has(key))
          .map((key) => `${type}.${key}`),
      ),
    );
    expect(unsaved).toEqual([]);
    const shownAsLines = Object.entries(DOC_FIELD_MAP).flatMap(
      ([type, specs]) =>
        specs!.flatMap((spec) =>
          spec.keys
            .filter((key) => lineKeys.get(type)?.has(key))
            .map((key) => `${type}.${key}`),
        ),
    );
    expect(shownAsLines).toEqual([]);
  });
});
