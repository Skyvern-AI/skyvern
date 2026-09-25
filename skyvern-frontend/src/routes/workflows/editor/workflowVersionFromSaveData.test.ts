import { describe, expect, test } from "vitest";
import { parse, stringify } from "yaml";

import { ProxyLocation } from "@/api/types";
import type { WorkflowSaveData } from "@/store/WorkflowHasChangesStore";

import type {
  WorkflowDefinition,
  WorkflowSettings,
} from "../types/workflowTypes";
import {
  preservedFinallyBlockLabel,
  YamlCommitError,
  workflowVersionFromSaveData,
  yamlCommitInputs,
} from "./workflowVersionFromSaveData";

const definition: WorkflowDefinition = {
  parameters: [],
  blocks: [],
};

function makeSaveData(
  settingsOverrides: Partial<WorkflowSettings> = {},
): WorkflowSaveData {
  const settings = {
    adaptiveCaching: true,
    generateScriptOnTerminal: false,
    totpIdentifier: null,
    totpVerificationUrl: null,
    proxyLocation: ProxyLocation.Residential,
    webhookCallbackUrl: "https://example.test/hook",
    persistBrowserSession: true,
    reuseBrowserSession: true,
    browserProfileId: "bp_1",
    browserProfileKey: "key_1",
    model: null,
    maxScreenshotScrolls: 5,
    maxElapsedTimeMinutes: 30,
    extraHttpHeaders: null,
    cdpConnectHeaders: null,
    runWith: "agent",
    codeVersion: null,
    scriptCacheKey: "cache_1",
    aiFallback: true,
    maskSecrets: true,
    runSequentially: false,
    sequentialKey: null,
    finallyBlockLabel: null,
    workflowSystemPrompt: null,
    errorCodeMapping: null,
    ...settingsOverrides,
  } as WorkflowSettings;
  return {
    parameters: [],
    blocks: [],
    workflowDefinitionVersion: 2,
    title: "My Workflow",
    description: "Live description",
    settings,
    workflow: {
      workflow_id: "w_1",
      organization_id: "org_1",
      workflow_permanent_id: "wpid_1",
      version: 3,
      description: "desc",
      is_saved_task: false,
      is_template: false,
      totp_verification_url: null,
      totp_identifier: null,
      status: "published",
      created_at: "2026-01-01T00:00:00Z",
      modified_at: "2026-01-02T00:00:00Z",
      deleted_at: null,
      adaptive_caching: true,
      folder_id: "f_1",
      import_error: "prior import failed",
    },
  } as unknown as WorkflowSaveData;
}

describe("workflowVersionFromSaveData", () => {
  test("carries identity and title, and uses the passed definition", () => {
    const version = workflowVersionFromSaveData(makeSaveData(), definition, {
      extraHttpHeaders: null,
      cdpConnectHeaders: null,
    });
    expect(version.workflow_id).toBe("w_1");
    expect(version.workflow_permanent_id).toBe("wpid_1");
    expect(version.title).toBe("My Workflow");
    expect(version.version).toBe(3);
    expect(version.workflow_definition).toBe(definition);
    // import_error is carried over, not reset (would otherwise clear after a
    // YAML commit on a workflow with a prior import error).
    expect(version.import_error).toBe("prior import failed");
  });

  test("projects live description, TOTP, and adaptive caching instead of loaded values", () => {
    const saveData = makeSaveData({
      totpIdentifier: "live-id",
      totpVerificationUrl: "https://example.test/totp",
      adaptiveCaching: false,
      generateScriptOnTerminal: false,
    });
    const headers = { extraHttpHeaders: null, cdpConnectHeaders: null };
    expect(
      workflowVersionFromSaveData(saveData, definition, headers),
    ).toMatchObject({
      description: "Live description",
      totp_identifier: "live-id",
      totp_verification_url: "https://example.test/totp",
      adaptive_caching: false,
    });
    expect(
      workflowVersionFromSaveData(
        { ...saveData, description: null },
        definition,
        headers,
      ).description,
    ).toBeNull();
  });

  test("maps editor settings onto the version", () => {
    const version = workflowVersionFromSaveData(makeSaveData(), definition, {
      extraHttpHeaders: { a: "b" },
      cdpConnectHeaders: null,
    });
    expect(version.proxy_location).toBe(ProxyLocation.Residential);
    expect(version.persist_browser_session).toBe(true);
    expect(version.reuse_browser_session).toBe(true);
    expect(version.browser_profile_id).toBe("bp_1");
    expect(version.cache_key).toBe("cache_1");
    expect(version.mask_secrets).toBe(true);
    expect(version.extra_http_headers).toEqual({ a: "b" });
    expect(version.adaptive_caching).toBe(true);
  });

  test("code_version is null for agent runs and defaults to 2 for code runs", () => {
    const agent = workflowVersionFromSaveData(makeSaveData(), definition, {
      extraHttpHeaders: null,
      cdpConnectHeaders: null,
    });
    expect(agent.run_with).toBe("agent");
    expect(agent.code_version).toBeNull();

    const code = workflowVersionFromSaveData(
      makeSaveData({ runWith: "code", codeVersion: null }),
      definition,
      { extraHttpHeaders: null, cdpConnectHeaders: null },
    );
    expect(code.run_with).toBe("code");
    expect(code.code_version).toBe(2);
  });
});

describe("preservedFinallyBlockLabel", () => {
  test("keeps the label when its block still exists", () => {
    expect(
      preservedFinallyBlockLabel("cleanup", ["step_1", "cleanup", "step_2"]),
    ).toBe("cleanup");
  });

  test("drops the label when the block was removed or renamed", () => {
    expect(preservedFinallyBlockLabel("cleanup", ["step_1", "step_2"])).toBe(
      null,
    );
    expect(preservedFinallyBlockLabel("cleanup", [])).toBe(null);
  });

  test("returns null for an unset finally_block_label", () => {
    expect(preservedFinallyBlockLabel(null, ["step_1"])).toBe(null);
    expect(preservedFinallyBlockLabel(undefined, ["step_1"])).toBe(null);
    expect(preservedFinallyBlockLabel("", ["step_1"])).toBe(null);
  });
});

describe("yamlCommitInputs", () => {
  test("unwraps a full export and preserves its parameters", () => {
    const nested = {
      version: 2,
      parameters: [
        {
          key: "target_url",
          parameter_type: "workflow",
          workflow_parameter_type: "string",
        },
      ],
      blocks: [
        {
          label: "open_target",
          block_type: "goto_url",
          url: "https://example.test/",
        },
      ],
    };
    const fullExport = {
      title: "wf",
      proxy_location: "RESIDENTIAL",
      workflow_definition: nested,
      run_sequentially: false,
    };
    const draft = "title: wf\nworkflow_definition:\n  parameters: []\n";

    const { definition, definitionYaml } = yamlCommitInputs(
      fullExport as unknown as typeof nested,
      draft,
    );
    expect(definition).toBe(nested);
    expect(definition.parameters).toHaveLength(1);
    expect(definition.parameters[0]?.key).toBe("target_url");
    expect(definitionYaml).not.toBe(draft);
    const roundTrip = parse(definitionYaml);
    expect(roundTrip.blocks).toEqual(nested.blocks);
    expect(roundTrip.parameters).toEqual(nested.parameters);
    expect(roundTrip.title).toBeUndefined();
  });

  test.each(["workflow_system_promt", "finally_block_lable"])(
    "rejects an unknown definition key with its exact path: %s",
    (key) => {
      const draft = stringify({
        workflow_definition: { parameters: [], blocks: [], [key]: "value" },
      });
      const commit = () => yamlCommitInputs(parse(draft), draft);
      expect.soft(commit).toThrowError(YamlCommitError);
      expect
        .soft(commit)
        .toThrowError(
          new YamlCommitError(`workflow_definition.${key}`, "unknown setting"),
        );
    },
  );

  test("round-trips every accepted definition key", () => {
    const definition = {
      version: 2,
      parameters: [
        {
          key: "target_url",
          parameter_type: "workflow",
          workflow_parameter_type: "string",
        },
      ],
      blocks: [
        { label: "cleanup", block_type: "goto_url", url: "{{ target_url }}" },
      ],
      finally_block_label: "cleanup",
      workflow_system_prompt: "Follow the workflow instructions",
      error_code_mapping: { FAILED: "Retry the workflow" },
      retry_policy: {
        max_retries: 2,
        delay_seconds: 10,
        webhook_on_retry: "final_only",
        retry_on: [{ status: "failed", error_codes: ["FAILED"] }],
      },
    };
    const draft = stringify({ workflow_definition: definition });
    const result = yamlCommitInputs(parse(draft), draft);
    expect(result.definition).toEqual(definition);
    expect(parse(result.definitionYaml)).toEqual(definition);
    expect(result.settingsPatch).toEqual({
      finally_block_label: definition.finally_block_label,
      workflow_system_prompt: definition.workflow_system_prompt,
      error_code_mapping: definition.error_code_mapping,
      retry_policy: definition.retry_policy,
    });
  });

  test("rejects Copilot-managed completion contracts in full documents", () => {
    const draft = stringify({
      workflow_definition: {
        parameters: [],
        blocks: [],
        completion_contract: { output: "result" },
      },
    });
    expect(() => yamlCommitInputs(parse(draft), draft)).toThrow(
      "workflow_definition.completion_contract: unknown setting",
    );
  });

  test("returns a definition-only draft byte-exact", () => {
    const draft =
      "parameters: []\nblocks:\n  # user's comment\n  - label: a\n    block_type: goto_url\n";
    const parsed = parse(draft);

    const { definition, definitionYaml } = yamlCommitInputs(parsed, draft);
    expect(definition).toBe(parsed);
    expect(definitionYaml).toBe(draft);
  });

  test.each([NaN, [], null, undefined, "scalar", 1])(
    "rejects a non-mapping root: %j",
    (input) => {
      expect(() => yamlCommitInputs(input, "raw")).toThrow(
        "document root must be a mapping",
      );
    },
  );

  test.each([
    [{ blocks: [], workflow_definition: { blocks: [] } }, "ambiguous document"],
    [{ title: "name" }, "workflow_definition is required in a full document"],
    [
      { description: null },
      "workflow_definition is required in a full document",
    ],
    [
      { workflow_definition: null },
      "workflow_definition must be a mapping with a blocks list",
    ],
    [
      { workflow_definition: "invalid" },
      "workflow_definition must be a mapping with a blocks list",
    ],
    [
      { workflow_definition: [] },
      "workflow_definition must be a mapping with a blocks list",
    ],
    [
      { workflow_definition: {} },
      "workflow_definition must be a mapping with a blocks list",
    ],
    [
      { workflow_definition: { blocks: null } },
      "workflow_definition must be a mapping with a blocks list",
    ],
    [{ parameters: [] }, "document must contain blocks or workflow_definition"],
    [{}, "document must contain blocks or workflow_definition"],
    [
      { workflow_definition: { blocks: [] }, surprise: true, typo: true },
      "surprise, typo",
    ],
    [{ title: null, workflow_definition: { blocks: [] } }, "title"],
    [{ title: "", workflow_definition: { blocks: [] } }, "title"],
    [{ description: 1, workflow_definition: { blocks: [] } }, "description"],
  ])("classifies invalid document %j", (input, message) => {
    expect(() => yamlCommitInputs(input, "raw")).toThrow(YamlCommitError);
    expect(() => yamlCommitInputs(input, "raw")).toThrow(message);
  });

  test.each([null, "invalid", 42])(
    "keeps malformed legacy blocks for endpoint validation: %j",
    (blocks) => {
      const parsed = { parameters: "invalid", blocks, arbitrary: true };
      expect(yamlCommitInputs(parsed, "# raw legacy\nblocks: invalid")).toEqual(
        {
          kind: "legacy",
          definition: parsed,
          definitionYaml: "# raw legacy\nblocks: invalid",
          settingsPatch: {},
          metadataPatch: {},
        },
      );
    },
  );

  test("extracts only own editable fields and ignores managed top-level keys", () => {
    const parsed = {
      workflow_definition: {
        blocks: [],
        retry_policy: null,
      },
      webhook_callback_url: null,
      cdp_connect_headers: { a: "b" },
      code_version: null,
      cache_key: null,
      title: "name",
      description: "",
      workflow_id: "ignored",
      organization_id: "ignored",
      workflow_permanent_id: "ignored",
      version: 10,
      created_at: "ignored",
      modified_at: "ignored",
      deleted_at: null,
      created_by: "ignored",
      edited_by: "ignored",
      copilot_authored: true,
      is_template: false,
      is_saved_task: true,
      status: "published",
      folder_id: "ignored",
      import_error: null,
    };
    const result = yamlCommitInputs(parsed, "raw");
    expect(result.kind).toBe("envelope");
    expect(result.settingsPatch).toEqual({
      webhook_callback_url: null,
      cdp_connect_headers: { a: "b" },
      code_version: null,
      cache_key: null,
      retry_policy: null,
    });
    expect(result.metadataPatch).toEqual({ title: "name", description: null });
    expect(parse(result.definitionYaml)).toEqual(parsed.workflow_definition);
  });
});
