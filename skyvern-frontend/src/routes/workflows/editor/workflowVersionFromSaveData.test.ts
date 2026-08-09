import { describe, expect, test } from "vitest";
import { parse } from "yaml";

import { ProxyLocation } from "@/api/types";
import type { WorkflowSaveData } from "@/store/WorkflowHasChangesStore";

import type {
  WorkflowDefinition,
  WorkflowSettings,
} from "../types/workflowTypes";
import {
  preservedFinallyBlockLabel,
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
    proxyLocation: ProxyLocation.Residential,
    webhookCallbackUrl: "https://example.test/hook",
    persistBrowserSession: true,
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

  test("maps editor settings onto the version", () => {
    const version = workflowVersionFromSaveData(makeSaveData(), definition, {
      extraHttpHeaders: { a: "b" },
      cdpConnectHeaders: null,
    });
    expect(version.proxy_location).toBe(ProxyLocation.Residential);
    expect(version.persist_browser_session).toBe(true);
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

  test("returns a definition-only draft byte-exact", () => {
    const draft =
      "parameters: []\nblocks:\n  # user's comment\n  - label: a\n    block_type: goto_url\n";
    const parsed = parse(draft);

    const { definition, definitionYaml } = yamlCommitInputs(parsed, draft);
    expect(definition).toBe(parsed);
    expect(definitionYaml).toBe(draft);
  });

  test("keeps raw text for a NaN draft", () => {
    expect(yamlCommitInputs(NaN, ".nan # keep").definitionYaml).toBe(
      ".nan # keep",
    );
  });

  test("prefers top-level blocks when both shapes are present", () => {
    const input = {
      blocks: [{ label: "a", block_type: "goto_url" }],
      workflow_definition: { parameters: [], blocks: [] },
    };
    const draft = "blocks: []\nworkflow_definition:\n  blocks: []\n";

    const { definition, definitionYaml } = yamlCommitInputs(input, draft);
    expect(definition).toBe(input);
    expect(definitionYaml).toBe(draft);
  });

  test("leaves malformed and non-object shapes alone", () => {
    const draft = "workflow_definition: invalid\n";
    const inputs = [
      { workflow_definition: "invalid" },
      { workflow_definition: [] },
      [],
      null,
      undefined,
    ];

    for (const input of inputs) {
      const { definition, definitionYaml } = yamlCommitInputs(input, draft);
      expect(definition).toBe(input);
      expect(definitionYaml).toBe(draft);
    }
  });
});
