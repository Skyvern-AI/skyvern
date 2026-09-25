import {
  clearDeferredEdits,
  deferredEdits,
} from "@/hooks/useDeferredLockedEdit";
import { EditorView } from "@codemirror/view";
import { WorkflowYamlEditor } from "./WorkflowYamlEditor";
import { confirmCodeCacheDeletion } from "./hooks/useSaveWorkflow";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { toast } from "@/components/ui/use-toast";
import {
  applyNodeChanges,
  ReactFlowProvider,
  type NodeChange,
} from "@xyflow/react";
import { MemoryRouter } from "react-router-dom";
import { WorkflowPermanentIdContext } from "../WorkflowPermanentIdContext";
import { SaveButton } from "./WorkflowHeader";
import type { AppNode } from "./nodes";
import {
  fireEvent,
  render,
  act,
  renderHook,
  waitFor,
} from "@testing-library/react";
import {
  useState,
  createElement,
  useLayoutEffect,
  type ReactNode,
} from "react";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useApplyRecordedBlocks } from "./recording/useApplyRecordedBlocks";
import {
  commitYamlDraft,
  isWorkflowYamlDirty,
  subscribeToYamlDraftChanges,
  beginCopilotAcceptance,
  beginSaveTransaction,
  finishSaveTransaction,
  finishCopilotAcceptance,
  registerEditorOwner,
  unregisterEditorOwner,
  beginYamlCommit,
  createYamlCommitOwner,
  finishYamlCommit,
  invalidateYamlCommitOwner,
  persistYamlCommitIfCurrent,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import {
  SaveRefusedError,
  SaveStaleError,
  useWorkflowHasChangesStore,
  useWorkflowSave,
} from "@/store/WorkflowHasChangesStore";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { parse, stringify } from "yaml";

import { ProxyLocation } from "@/api/types";
import {
  applyYamlCommitMetadata,
  useWorkflowTitleStore,
} from "@/store/WorkflowTitleStore";

import type { WorkflowApiResponse } from "../types/workflowTypes";
import type { BlockYAML, ParameterYAML } from "../types/workflowYamlTypes";
import { apiWorkflowToSettings } from "./apiWorkflowToSettings";
import {
  applySettingsPatch,
  validateSettingsPatch,
  resolveFinallyBlockLabel,
  buildWorkflowYamlDocument,
  buildWorkflowSaveRequest,
  buildWorkflowCopilotContext,
  restoreWorkflowCopilotSettings,
} from "./workflowYamlDocument";
import {
  normalizeRetryPolicy,
  validateRetryPolicyInput,
} from "./nodes/StartNode/retryPolicyUtils";
import {
  YamlCommitError,
  yamlCommitInputs,
  workflowVersionFromSaveData,
} from "./workflowVersionFromSaveData";

import {
  useWorkflowGraphState,
  createNode,
  getElements,
  getWorkflowBlocks,
  getWorkflowSettings,
  convert,
} from "./workflowEditorUtils";

const { put } = vi.hoisted(() => ({ put: vi.fn() }));
vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));
vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({ put }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn() }),
}));

beforeEach(() => {
  sessionStorage.clear();
});

const workflow: WorkflowApiResponse = {
  workflow_id: "w_fixture",
  organization_id: "o_fixture",
  workflow_permanent_id: "wpid_fixture",
  title: "Loaded title",
  description: "Loaded description",
  version: 1,
  workflow_definition: { parameters: [], blocks: [] },
  proxy_location: ProxyLocation.Residential,
  webhook_callback_url: null,
  extra_http_headers: null,
  cdp_connect_headers: null,
  persist_browser_session: false,
  reuse_browser_session: false,
  pin_saved_session_ip: false,
  model: null,
  totp_verification_url: null,
  totp_identifier: null,
  max_screenshot_scrolls: null,
  max_elapsed_time_minutes: null,
  status: "published",
  created_at: "2026-01-01T00:00:00Z",
  modified_at: "2026-01-01T00:00:00Z",
  deleted_at: null,
  run_with: "agent",
  cache_key: null,
  ai_fallback: null,
  enable_self_healing: null,
  adaptive_caching: null,
  code_version: null,
  mask_secrets: false,
  run_sequentially: false,
  sequential_key: null,
  folder_id: "folder_fixture",
  import_error: null,
  is_saved_task: true,
  is_template: false,
};

function input() {
  return {
    workflow,
    settings: apiWorkflowToSettings(workflow),
    title: "Draft title",
    description: "Draft description" as string | null,
    parameters: [] as ParameterYAML[],
    blocks: [] as BlockYAML[],
    definitionVersion: 2,
  };
}

const topLevelKeys = [
  "title",
  "description",
  "webhook_callback_url",
  "proxy_location",
  "persist_browser_session",
  "reuse_browser_session",
  "pin_saved_session_ip",
  "browser_profile_id",
  "browser_profile_key",
  "model",
  "max_screenshot_scrolls",
  "max_elapsed_time_minutes",
  "extra_http_headers",
  "cdp_connect_headers",
  "run_with",
  "browser_type",
  "code_version",
  "cache_key",
  "ai_fallback",
  "mask_secrets",
  "run_sequentially",
  "sequential_key",
  "totp_verification_url",
  "totp_identifier",
  "adaptive_caching",
  "generate_script_on_terminal",
  "workflow_definition",
];

describe("apiWorkflowToSettings", () => {
  test("preserves a stored null proxy location", () => {
    expect(
      apiWorkflowToSettings({ ...workflow, proxy_location: null })
        .proxyLocation,
    ).toBeNull();
  });
});

describe("buildWorkflowYamlDocument", () => {
  test("round-trips a null proxy through YAML, canvas, save, and Copilot context", () => {
    const data = input();
    data.settings.proxyLocation = null;
    const yaml = stringify(buildWorkflowYamlDocument(data));
    expect(yaml).toContain("proxy_location: null");
    const { settingsPatch } = yamlCommitInputs(parse(yaml), yaml);
    expect(settingsPatch).toHaveProperty("proxy_location", null);
    expect(() => validateSettingsPatch(settingsPatch)).not.toThrow();
    const settings = applySettingsPatch(input().settings, settingsPatch);
    expect(settings.proxyLocation).toBeNull();
    const { nodes } = getElements([], settings, true);
    const saveData = {
      ...data,
      settings: getWorkflowSettings(nodes),
      workflowDefinitionVersion: data.definitionVersion,
    };
    expect(saveData.settings.proxyLocation).toBeNull();
    expect(parse(stringify(buildWorkflowSaveRequest(saveData)))).toHaveProperty(
      "proxy_location",
      null,
    );
    const version = workflowVersionFromSaveData(
      saveData,
      workflow.workflow_definition,
      { extraHttpHeaders: null, cdpConnectHeaders: null },
    );
    expect(apiWorkflowToSettings(version).proxyLocation).toBeNull();
    const context = buildWorkflowCopilotContext(saveData);
    expect(parse(stringify(context.document))).toHaveProperty(
      "proxy_location",
      null,
    );
    expect(context.snapshot).toHaveProperty("proxy_location", null);
  });

  test.each([{ country: "US" }, { country: "us", isISP: false }])(
    "keeps an applied country-only proxy %j when the snapshot proxy is null",
    (proxy) => {
      const snapshot = apiWorkflowToSettings({
        ...workflow,
        proxy_location: null,
      });
      const applied = { ...workflow, proxy_location: proxy };
      const restored = restoreWorkflowCopilotSettings(applied, snapshot);
      expect(restored.settings.proxyLocation).toEqual(proxy);
      expect(restored.settingsChanged).toBe(false);
    },
  );

  test("round-trips browser type through YAML, canvas, save, hydration, and public Copilot context", () => {
    const stored = { ...workflow, browser_type: "chromium" };
    const data = {
      ...input(),
      workflow: stored,
      settings: apiWorkflowToSettings(stored),
    };
    const document = parse(stringify(buildWorkflowYamlDocument(data)));
    expect(document.browser_type).toBe("chromium");
    expect(convert(stored).browser_type).toBe("chromium");

    for (const browserType of ["chrome", "future-browser", null]) {
      document.browser_type = browserType;
      const yaml = stringify(document);
      const parsed = yamlCommitInputs(parse(yaml), yaml);
      const settings = applySettingsPatch(data.settings, parsed.settingsPatch);
      const { nodes } = getElements([], settings, true);
      const saveData = {
        ...data,
        settings: getWorkflowSettings(nodes),
        workflowDefinitionVersion: data.definitionVersion,
      };
      const request = parse(stringify(buildWorkflowSaveRequest(saveData)));
      expect(request.browser_type).toBe(browserType);
      const version = workflowVersionFromSaveData(
        saveData,
        stored.workflow_definition,
        { extraHttpHeaders: null, cdpConnectHeaders: null },
      );
      expect(apiWorkflowToSettings(version).browserType).toBe(browserType);
      const context = buildWorkflowCopilotContext(saveData);
      expect(context.document.browser_type).toBe(browserType);
      expect(context.snapshot.browser_type).toBe(browserType);
      expect(
        restoreWorkflowCopilotSettings(version, data.settings).settings
          .browserType,
      ).toBe(browserType);
    }

    delete document.browser_type;
    const yaml = stringify(document);
    expect(
      applySettingsPatch(
        data.settings,
        yamlCommitInputs(parse(yaml), yaml).settingsPatch,
      ).browserType,
    ).toBe("chromium");
    expect(() =>
      applySettingsPatch(data.settings, { browser_type: 42 }),
    ).toThrow("browser_type: must be a string or null");
  });

  test("preserves terminal script generation through YAML, canvas, and save, and accepts an edit", () => {
    const stored = { ...workflow, generate_script_on_terminal: true };
    const data = {
      ...input(),
      workflow: stored,
      settings: apiWorkflowToSettings(stored),
    };
    const document = parse(stringify(buildWorkflowYamlDocument(data)));
    expect(document.generate_script_on_terminal).toBe(true);
    expect(convert(stored).generate_script_on_terminal).toBe(true);
    for (const edited of [true, false]) {
      document.generate_script_on_terminal = edited;
      const yaml = stringify(document);
      const parsed = yamlCommitInputs(parse(yaml), yaml);
      const settings = applySettingsPatch(data.settings, parsed.settingsPatch);
      const { nodes } = getElements([], settings, true);
      const saveData = {
        ...data,
        settings: getWorkflowSettings(nodes),
        workflowDefinitionVersion: 2,
      };
      expect(
        parse(stringify(buildWorkflowSaveRequest(saveData)))
          .generate_script_on_terminal,
      ).toBe(edited);
      const version = workflowVersionFromSaveData(
        saveData,
        stored.workflow_definition,
        { extraHttpHeaders: null, cdpConnectHeaders: null },
      );
      expect(apiWorkflowToSettings(version).generateScriptOnTerminal).toBe(
        edited,
      );
    }
    expect(applySettingsPatch(data.settings, {}).generateScriptOnTerminal).toBe(
      true,
    );
    expect(
      applySettingsPatch(data.settings, { generate_script_on_terminal: null })
        .generateScriptOnTerminal,
    ).toBe(false);
    expect(() =>
      applySettingsPatch(data.settings, {
        generate_script_on_terminal: "false",
      }),
    ).toThrow("generate_script_on_terminal");
  });

  test.each([
    {
      proxy: { url: "http://test-user:test-password@proxy.example.test" },
      editedProxy: { url: "http://test-user:new-password@proxy.example.test" },
    },
    {
      proxy: {
        url: "http://proxy.example.test",
        unexpected: true,
      },
      editedProxy: {
        url: "http://proxy.example.test",
        unexpected: false,
      },
    },
    {
      proxy: { url: "http://u:p@h:1" },
      editedProxy: { url: "http://proxy.example.test" },
    },
  ])(
    "withholds private proxy $proxy from Copilot and preserves it through a turn, Accept, and YAML edits",
    ({ proxy, editedProxy }) => {
      const stored = {
        ...workflow,
        proxy_location: proxy,
      } as unknown as WorkflowApiResponse;
      const data = { ...input(), settings: apiWorkflowToSettings(stored) };
      const context = buildWorkflowCopilotContext(data);
      expect(context.document).not.toHaveProperty("proxy_location");
      expect(parse(stringify(context.document))).not.toHaveProperty(
        "proxy_location",
      );
      expect(stringify(context.document)).not.toContain("test-password");
      expect(context.snapshot.proxy_location).toEqual(proxy);

      const restored = restoreWorkflowCopilotSettings(
        { ...stored, proxy_location: null },
        data.settings,
      );
      expect(restored.settings.proxyLocation).toEqual(proxy);
      expect(restored.settingsChanged).toBe(true);
      const { nodes } = getElements([], restored.settings, true);
      expect(
        buildWorkflowSaveRequest({
          ...data,
          settings: getWorkflowSettings(nodes),
          workflowDefinitionVersion: data.definitionVersion,
        }).proxy_location,
      ).toEqual(proxy);

      const document = parse(stringify(buildWorkflowYamlDocument(data)));
      expect(document.proxy_location).toEqual(proxy);
      document.proxy_location = editedProxy;
      const yaml = stringify(document);
      const parsed = yamlCommitInputs(parse(yaml), yaml);
      const settings = applySettingsPatch(data.settings, parsed.settingsPatch);
      expect(
        buildWorkflowSaveRequest({
          ...data,
          settings,
          workflowDefinitionVersion: data.definitionVersion,
        }).proxy_location,
      ).toEqual(editedProxy);
    },
  );

  test.each([
    {},
    { foo: "bar" },
    {
      server: "http://proxy.example.test",
      username: "test-user",
      password: "test-password",
    },
    { subdivision: "x" },
    { city: "New York" },
    { country: "US", password: "test-password" },
    { country: 1 },
    { country: "United States" },
    { country: "ZZ" },
    { subdivision: null },
    { city: { password: "test-password" } },
    { isISP: "test-password" },
    { isISP: null },
    { country: "US", city: 1 },
    { country: "US", isISP: "yes" },
    { country: "US", unexpected: "value" },
    { url: null },
    { url: 123 },
    { url: "http://proxy.example.test", country: "US" },
    { url: "http://proxy.example.test", country: null },
  ])(
    "rejects edits but withholds and restores stored malformed proxies: %j",
    (proxy) => {
      const stored = {
        ...workflow,
        proxy_location: proxy,
      } as unknown as WorkflowApiResponse;
      const settings = apiWorkflowToSettings(stored);
      const context = buildWorkflowCopilotContext({ ...input(), settings });
      expect(parse(stringify(context.document))).not.toHaveProperty(
        "proxy_location",
      );
      expect(context.snapshot.proxy_location).toEqual(proxy);
      expect(() =>
        applySettingsPatch(settings, { proxy_location: proxy }),
      ).toThrowError(
        new YamlCommitError(
          "proxy_location",
          "unsupported proxy_location value in the editor",
        ),
      );
      for (const appliedProxy of [null, undefined]) {
        const applied = { ...workflow } as Partial<WorkflowApiResponse>;
        if (appliedProxy === undefined) delete applied.proxy_location;
        else applied.proxy_location = appliedProxy;
        const restored = restoreWorkflowCopilotSettings(
          applied as WorkflowApiResponse,
          settings,
        );
        expect(restored.settings.proxyLocation).toEqual(proxy);
        expect(restored.settingsChanged).toBe(true);
      }
    },
  );

  test.each([
    ProxyLocation.Residential,
    { country: "US" },
    { country: "us" },
    { country: "US", isISP: true },
    { country: "US", isISP: false },
    null,
  ])("keeps public proxy %j in Copilot and allows updates", (proxy) => {
    const data = input();
    data.settings.proxyLocation = proxy;
    const context = buildWorkflowCopilotContext(data);
    expect(parse(stringify(context.document)).proxy_location).toEqual(proxy);
    expect(() =>
      validateSettingsPatch({ proxy_location: proxy }),
    ).not.toThrow();
    const applied = { ...workflow, proxy_location: null };
    const restored = restoreWorkflowCopilotSettings(applied, data.settings);
    expect(restored.settings.proxyLocation).toBeNull();
    expect(restored.settingsChanged).toBe(false);

    const omitted = { ...workflow } as Partial<WorkflowApiResponse>;
    delete omitted.proxy_location;
    const unchanged = restoreWorkflowCopilotSettings(
      omitted as WorkflowApiResponse,
      data.settings,
    );
    expect(unchanged.settings.proxyLocation).toEqual(proxy);
    expect(unchanged.settingsChanged).toBe(proxy !== null);
  });

  test.each([
    {
      country: "US",
      subdivision: "CA",
      city: "San Francisco",
      isISP: true,
    },
    { country: "US", subdivision: "CA", city: "San Francisco" },
    {
      country: "us",
      subdivision: "CA",
      city: "San Francisco",
      isISP: true,
    },
    {
      country: "US",
      subdivision: "CA",
      city: "San Francisco",
      isISP: false,
    },
    { country: "US", subdivision: null, city: null },
    { country: "US", subdivision: "CA" },
    { country: "US", city: "San Francisco" },
  ])(
    "preserves the full geo target in Copilot context and through an unchanged proposal: %j",
    (proxy) => {
      const stored = { ...workflow, proxy_location: proxy as ProxyLocation };
      const data = {
        ...input(),
        workflow: stored,
        settings: apiWorkflowToSettings(stored),
      };
      const context = buildWorkflowCopilotContext(data);
      expect(context.document.proxy_location).toEqual(proxy);
      const proposal = parse(stringify(context.document));
      expect(proposal.proxy_location).toEqual(proxy);
      expect(context.snapshot.proxy_location).toEqual(proxy);
      expect(data.settings.proxyLocation).toEqual(proxy);
      const applied = { ...workflow, proxy_location: proposal.proxy_location };
      const restored = restoreWorkflowCopilotSettings(applied, data.settings);
      expect(restored.settings.proxyLocation).toEqual(proxy);
      expect(restored.settingsChanged).toBe(false);
      const { nodes } = getElements([], restored.settings, true);
      expect(
        buildWorkflowSaveRequest({
          ...data,
          settings: getWorkflowSettings(nodes),
          workflowDefinitionVersion: data.definitionVersion,
        }).proxy_location,
      ).toEqual(proxy);
    },
  );

  test.each([
    { country: "us", isISP: false },
    { country: "US" },
    { country: "US", city: null },
    { country: "US", subdivision: null },
  ])(
    "keeps a country-only proposal without reattaching omitted or null geo fields: %j",
    (appliedProxy) => {
      const snapshot = {
        ...input().settings,
        proxyLocation: {
          country: "US",
          subdivision: "CA",
          city: "San Francisco",
          isISP: true,
        },
      };
      const restored = restoreWorkflowCopilotSettings(
        {
          ...workflow,
          proxy_location: appliedProxy,
        } as unknown as WorkflowApiResponse,
        snapshot,
      );
      expect(restored.settings.proxyLocation).toEqual(appliedProxy);
      expect(restored.settingsChanged).toBe(false);
    },
  );

  test.each([null, "NY"])(
    "keeps explicit null geo fields with applied subdivision %j",
    (subdivision) => {
      const snapshot = {
        ...input().settings,
        proxyLocation: {
          country: "US",
          subdivision: "CA",
          city: "San Francisco",
        },
      };
      const appliedProxy = { country: "US", subdivision, city: null };
      const restored = restoreWorkflowCopilotSettings(
        {
          ...workflow,
          proxy_location: appliedProxy,
        } as unknown as WorkflowApiResponse,
        snapshot,
      );
      expect(restored.settings.proxyLocation).toEqual(appliedProxy);
      expect(restored.settingsChanged).toBe(false);
    },
  );

  test.each([
    { country: "CA", isISP: true },
    { country: "US", city: "applied-city" },
    { country: "US", subdivision: "applied-region" },
    { country: "US", city: "" },
    { country: "US", subdivision: "" },
    { url: "http://proxy.example.test" },
    { country: "US", isISP: "invalid" },
    ProxyLocation.Residential,
    null,
  ])(
    "keeps applied proxies with a different country, shape, or explicit geo fields: %j",
    (appliedProxy) => {
      const snapshot = {
        ...input().settings,
        proxyLocation: {
          country: "US",
          subdivision: "private-region",
          city: "private-city",
          isISP: true,
        },
      };
      const applied = {
        ...workflow,
        proxy_location: appliedProxy,
      } as unknown as WorkflowApiResponse;
      const settings = apiWorkflowToSettings(applied);
      const restored = restoreWorkflowCopilotSettings(
        applied,
        snapshot,
        settings,
      );
      expect(restored).toEqual({ settings, settingsChanged: false });
    },
  );

  test.each([
    null,
    undefined,
    ProxyLocation.Residential,
    { country: "US" },
    { country: "us", isISP: false },
    { url: "http://different-proxy.example.test" },
    { url: "http://test-user:test-password@proxy.example.test" },
    {
      url: "http://test-user:test-password@proxy.example.test",
      password: "new-password",
    },
  ])(
    "restores a withheld custom proxy only for a null or absent proposal: %j",
    (appliedProxy) => {
      const proxy = {
        url: "http://test-user:test-password@proxy.example.test",
      };
      const stored = {
        ...workflow,
        proxy_location: proxy,
      } as unknown as WorkflowApiResponse;
      const snapshot = apiWorkflowToSettings(stored);
      expect(
        buildWorkflowCopilotContext({ ...input(), settings: snapshot })
          .document,
      ).not.toHaveProperty("proxy_location");
      const applied = {
        ...workflow,
        proxy_location: appliedProxy,
      } as unknown as WorkflowApiResponse;
      if (appliedProxy === undefined) {
        delete (applied as Partial<WorkflowApiResponse>).proxy_location;
      }
      const restored = restoreWorkflowCopilotSettings(applied, snapshot);
      const expectedProxy = appliedProxy ?? proxy;
      expect(restored.settings.proxyLocation).toEqual(expectedProxy);
      expect(restored.settingsChanged).toBe(appliedProxy == null);
      expect(
        buildWorkflowSaveRequest({
          ...input(),
          settings: restored.settings,
          workflowDefinitionVersion: 2,
        }).proxy_location,
      ).toEqual(expectedProxy);
    },
  );

  test.each([
    {
      appliedProxy: {
        credentials: { password: "test-password", username: "test-user" },
        ports: [80, 443],
      },
    },
    {
      appliedProxy: {
        credentials: { password: "new-password", username: "test-user" },
        ports: [80, 443],
      },
    },
    {
      appliedProxy: {
        credentials: { password: "test-password", username: "test-user" },
        ports: [443, 80],
      },
    },
  ])(
    "keeps an explicit non-null proxy mapping after applying $appliedProxy",
    ({ appliedProxy }) => {
      const proxy = {
        ports: [80, 443],
        credentials: { username: "test-user", password: "test-password" },
      };
      const stored = {
        ...workflow,
        proxy_location: proxy,
      } as unknown as WorkflowApiResponse;
      const applied = {
        ...workflow,
        proxy_location: appliedProxy,
      } as unknown as WorkflowApiResponse;
      const restored = restoreWorkflowCopilotSettings(
        applied,
        apiWorkflowToSettings(stored),
      );
      expect(restored.settings.proxyLocation).toEqual(appliedProxy);
      expect(restored.settingsChanged).toBe(false);
    },
  );

  test("marks a restored custom proxy dirty when persistence returned null", () => {
    const snapshot = apiWorkflowToSettings({
      ...workflow,
      proxy_location: { url: "http://proxy.example.test" },
    } as unknown as WorkflowApiResponse);
    const restored = restoreWorkflowCopilotSettings(
      { ...workflow, proxy_location: null },
      snapshot,
    );
    expect(restored.settings.proxyLocation).toEqual(snapshot.proxyLocation);
    expect(restored.settingsChanged).toBe(true);
  });

  test.each([
    ["extraHttpHeaders", "extra_http_headers"],
    ["cdpConnectHeaders", "cdp_connect_headers"],
  ] as const)(
    "keeps %s strict for saves and safe for Copilot",
    (setting, field) => {
      const data = input();
      data.settings[setting] = '{"Authorization":"raw-token"}';
      data.settings.totpIdentifier = "private-totp-id";
      data.settings.totpVerificationUrl =
        setting === "extraHttpHeaders"
          ? "https://example.test/totp?token=private-token"
          : "https://test-user:test-password@example.test/totp";
      data.settings.webhookCallbackUrl =
        "https://example.test/webhook/private-path?signature=private-value";
      const context = buildWorkflowCopilotContext(data);
      for (const [field, setting] of [
        ["totp_identifier", "totpIdentifier"],
        ["totp_verification_url", "totpVerificationUrl"],
        ["webhook_callback_url", "webhookCallbackUrl"],
      ] as const) {
        expect.soft(context.document).not.toHaveProperty(field);
        expect
          .soft(parse(stringify(context.document)))
          .not.toHaveProperty(field);
        expect(context.snapshot[field]).toBe(data.settings[setting]);
      }
      expect(context.document).not.toHaveProperty(field);
      expect(parse(stringify(context.document))).not.toHaveProperty(field);
      expect(context.snapshot[field]).toEqual({ Authorization: "raw-token" });
      data.settings[setting] = '{"Authorization":';
      expect(() =>
        buildWorkflowSaveRequest({
          ...data,
          workflowDefinitionVersion: data.definitionVersion,
        }),
      ).toThrow(field);
      expect(() => buildWorkflowYamlDocument(data)).toThrow(field);
      expect(buildWorkflowCopilotContext(data).document[field]).toBeUndefined();
    },
  );

  test.each(["null", "absent", "matching"] as const)(
    "restores withheld headers, TOTP, and webhook settings from a %s response",
    (response) => {
      const data = input();
      data.settings.extraHttpHeaders = '{"Authorization":"test-token"}';
      data.settings.cdpConnectHeaders = '{"X-Test-Header":"test-value"}';
      data.settings.totpIdentifier = "test-totp-id";
      data.settings.totpVerificationUrl = "https://example.test/totp";
      data.settings.webhookCallbackUrl = "https://example.test/webhook";
      const context = buildWorkflowCopilotContext(data);
      const applied: Partial<WorkflowApiResponse> = { ...workflow };
      for (const field of [
        "extra_http_headers",
        "cdp_connect_headers",
        "totp_identifier",
        "totp_verification_url",
        "webhook_callback_url",
      ] as const) {
        expect(context.document).not.toHaveProperty(field);
        if (response === "absent") delete applied[field];
        else if (response === "matching") {
          Object.assign(applied, { [field]: context.snapshot[field] });
        } else applied[field] = null;
      }
      const restored = restoreWorkflowCopilotSettings(
        applied as WorkflowApiResponse,
        data.settings,
      );
      expect(restored.settings).toMatchObject({
        extraHttpHeaders: data.settings.extraHttpHeaders,
        cdpConnectHeaders: data.settings.cdpConnectHeaders,
        totpIdentifier: data.settings.totpIdentifier,
        totpVerificationUrl: data.settings.totpVerificationUrl,
        webhookCallbackUrl: data.settings.webhookCallbackUrl,
      });
      expect(restored.settingsChanged).toBe(response !== "matching");
    },
  );

  test.each(["null", "absent", "matching", "different"] as const)(
    "restores the withheld webhook from a %s response and tracks unsaved changes",
    (response) => {
      const data = input();
      data.settings.webhookCallbackUrl =
        "https://example.test/webhook?signature=local-value";
      const applied: Partial<WorkflowApiResponse> = {
        ...workflow,
        webhook_callback_url: null,
      };
      if (response === "absent") delete applied.webhook_callback_url;
      else if (response === "matching") {
        applied.webhook_callback_url = data.settings.webhookCallbackUrl;
      } else if (response === "different") {
        applied.webhook_callback_url =
          "https://example.test/webhook?signature=other-value";
      }
      const restored = restoreWorkflowCopilotSettings(
        applied as WorkflowApiResponse,
        data.settings,
      );
      expect(restored.settings.webhookCallbackUrl).toBe(
        data.settings.webhookCallbackUrl,
      );
      expect(restored.settingsChanged).toBe(response !== "matching");
    },
  );

  test.each([
    [null, false],
    [undefined, false],
    ["https://example.test/webhook?signature=unsolicited-value", true],
  ] as const)(
    "preserves a null webhook snapshot when the response carries %j",
    (appliedUrl, settingsChanged) => {
      const applied: Partial<WorkflowApiResponse> = { ...workflow };
      if (appliedUrl === undefined) delete applied.webhook_callback_url;
      else applied.webhook_callback_url = appliedUrl;
      const restored = restoreWorkflowCopilotSettings(
        applied as WorkflowApiResponse,
        input().settings,
      );
      expect(restored.settings.webhookCallbackUrl).toBeNull();
      expect(restored.settingsChanged).toBe(settingsChanged);
    },
  );

  test.each([
    "https://test-user:test-password@example.test/webhook",
    "https://example.test/webhook/private-signing-value",
    "https://example.test/webhook?signature=private-value",
    null,
  ])(
    "round-trips webhook %j through YAML edits, canvas, save, and hydration",
    (url) => {
      const stored = {
        ...workflow,
        webhook_callback_url: "https://example.test/webhook/original",
      };
      const data = {
        ...input(),
        workflow: stored,
        settings: apiWorkflowToSettings(stored),
      };
      const document = parse(stringify(buildWorkflowYamlDocument(data)));
      expect(document.webhook_callback_url).toBe(stored.webhook_callback_url);
      document.webhook_callback_url = url;
      const yaml = stringify(document);
      const { settingsPatch } = yamlCommitInputs(parse(yaml), yaml);
      const settings = applySettingsPatch(data.settings, settingsPatch);
      expect(settings.webhookCallbackUrl).toBe(url);
      const { nodes } = getElements([], settings, true);
      const saveData = {
        ...data,
        settings: getWorkflowSettings(nodes),
        workflowDefinitionVersion: data.definitionVersion,
      };
      expect(
        parse(stringify(buildWorkflowSaveRequest(saveData))),
      ).toHaveProperty("webhook_callback_url", url);
      const version = workflowVersionFromSaveData(
        saveData,
        stored.workflow_definition,
        { extraHttpHeaders: null, cdpConnectHeaders: null },
      );
      expect(apiWorkflowToSettings(version).webhookCallbackUrl).toBe(url);
      const context = buildWorkflowCopilotContext(saveData);
      expect(context.snapshot).toHaveProperty("webhook_callback_url", url);
      expect(parse(stringify(context.document))).not.toHaveProperty(
        "webhook_callback_url",
      );
    },
  );

  test("emits every editable key and omits managed and read-only fields", () => {
    const document = parse(stringify(buildWorkflowYamlDocument(input())));
    expect(Object.keys(document).sort()).toEqual([...topLevelKeys].sort());
    expect(Object.keys(document.workflow_definition).sort()).toEqual(
      [
        "version",
        "parameters",
        "blocks",
        "finally_block_label",
        "workflow_system_prompt",
        "error_code_mapping",
        "retry_policy",
      ].sort(),
    );
    expect(document.title).toBe("Draft title");
    expect(document.description).toBe("Draft description");
    expect(document.workflow_definition.version).toBe(2);
    for (const field of [
      "is_saved_task",
      "status",
      "folder_id",
      "workflow_id",
      "organization_id",
      "version",
    ]) {
      expect(document).not.toHaveProperty(field);
    }
  });

  test("emits live null values instead of dropping keys or applying save defaults", () => {
    const draft = input();
    draft.description = null;
    draft.settings.aiFallback = null;
    draft.settings.codeVersion = null;
    const document = parse(stringify(buildWorkflowYamlDocument(draft)));
    for (const field of [
      "description",
      "webhook_callback_url",
      "browser_profile_id",
      "browser_profile_key",
      "model",
      "max_screenshot_scrolls",
      "max_elapsed_time_minutes",
      "extra_http_headers",
      "cdp_connect_headers",
      "code_version",
      "sequential_key",
      "totp_verification_url",
      "totp_identifier",
      "ai_fallback",
    ]) {
      expect(document[field], field).toBeNull();
    }
    for (const field of [
      "finally_block_label",
      "workflow_system_prompt",
      "error_code_mapping",
      "retry_policy",
    ]) {
      expect(document.workflow_definition[field], field).toBeNull();
    }
  });

  test.each([null, "", "custom-key"])(
    "normalizes cache_key %j for the document",
    (key) => {
      const draft = input();
      draft.settings.scriptCacheKey = key;
      expect(buildWorkflowYamlDocument(draft).cache_key).toBe(key || "default");
    },
  );

  test.each([null, "", "Draft description"])(
    "normalizes description %j",
    (description) => {
      expect(
        buildWorkflowYamlDocument({ ...input(), description }).description,
      ).toBe(description || null);
    },
  );

  test("uses live settings and preserves zero, headers, and code version in agent mode", () => {
    const draft = input();
    Object.assign(draft.settings, {
      maxScreenshotScrolls: 0,
      codeVersion: 1,
      extraHttpHeaders: '{"X-Test":"header"}',
      cdpConnectHeaders: '{"Authorization":"********"}',
      totpIdentifier: "draft-identifier",
      totpVerificationUrl: "https://example.test/totp",
      adaptiveCaching: true,
      generateScriptOnTerminal: false,
      errorCodeMapping: { RETRY: "Retry the request" },
    });
    expect(buildWorkflowYamlDocument(draft)).toMatchObject({
      max_screenshot_scrolls: 0,
      code_version: 1,
      run_with: "agent",
      extra_http_headers: { "X-Test": "header" },
      cdp_connect_headers: { Authorization: "********" },
      totp_identifier: "draft-identifier",
      totp_verification_url: "https://example.test/totp",
      adaptive_caching: true,
      workflow_definition: {
        error_code_mapping: { RETRY: "Retry the request" },
      },
    });
  });

  test.each([
    ["extraHttpHeaders", "extra_http_headers"],
    ["cdpConnectHeaders", "cdp_connect_headers"],
  ] as const)(
    "names %s when header JSON cannot be parsed",
    (setting, field) => {
      const draft = input();
      draft.settings[setting] = "{";
      expect(() => buildWorkflowYamlDocument(draft)).toThrow(YamlCommitError);
      expect(() => buildWorkflowYamlDocument(draft)).toThrow(field);
      try {
        buildWorkflowYamlDocument(draft);
      } catch (error) {
        expect(error).toMatchObject({ field });
      }
    },
  );

  test("keeps already-converted parameters and nested loop parameter_keys untouched", () => {
    const draft = input();
    draft.parameters = [
      {
        parameter_type: "workflow",
        key: "items",
        workflow_parameter_type: "json",
      },
    ];
    draft.blocks = [
      {
        block_type: "for_loop",
        label: "Loop",
        loop_over_parameter_key: "items",
        loop_variable_reference: "items",
        complete_if_empty: false,
        data_schema: null,
        loop_blocks: [
          {
            block_type: "code",
            label: "Read item",
            code: "result = items",
            error_code_mapping: null,
            parameter_keys: ["items"],
          },
        ],
      },
    ];
    const before = structuredClone(draft);
    const document = buildWorkflowYamlDocument(draft);
    expect(document.workflow_definition.blocks).toBe(draft.blocks);
    expect(document.workflow_definition.parameters).toBe(draft.parameters);
    expect(parse(stringify(document)).workflow_definition.blocks).toEqual(
      draft.blocks,
    );
    expect(draft).toEqual(before);
  });
});

describe("settings patch validation and merge", () => {
  test.each([
    { country: "US" },
    { country: "us" },
    { country: "US", subdivision: null, city: null },
    { country: "US", subdivision: "CA", city: "San Francisco" },
    { country: "US", isISP: true },
    { country: "US", isISP: false },
  ])("preserves GeoTarget %j through YAML validation and save", (proxy) => {
    const draft = input();
    draft.settings.proxyLocation = proxy as ProxyLocation;
    const document = buildWorkflowYamlDocument(draft);
    expect(document.proxy_location).toBe(proxy);
    const yaml = stringify(document);
    const { settingsPatch } = yamlCommitInputs(parse(yaml), yaml);
    expect(() => validateSettingsPatch(settingsPatch)).not.toThrow();
    const settings = applySettingsPatch(draft.settings, settingsPatch);
    const request = buildWorkflowSaveRequest({
      ...draft,
      settings,
      workflowDefinitionVersion: 2,
    });
    expect(request.proxy_location).toBe(settings.proxyLocation);
    expect(parse(stringify(request)).proxy_location).toEqual(proxy);
  });

  test.each([["mask_secrets", "maskSecrets"]] as const)(
    "null inherits %s while explicit false disables it",
    (field, setting) => {
      const draft = input();
      draft.settings[setting] = true;
      const document = buildWorkflowYamlDocument(draft);
      document[field] = null;
      const yaml = stringify(document);
      const parsed = yamlCommitInputs(parse(yaml), yaml);
      const settings = applySettingsPatch(draft.settings, parsed.settingsPatch);
      expect(settings[setting]).toBe(true);
      expect(
        buildWorkflowSaveRequest({
          ...draft,
          settings,
          workflowDefinitionVersion: 2,
        })[field],
      ).toBe(true);
      expect(applySettingsPatch(settings, { [field]: false })[setting]).toBe(
        false,
      );
    },
  );

  test.each([true, false, null])(
    "accepts and drops a legacy enable_self_healing: %j",
    (legacy) => {
      const draft = input();
      const document = {
        ...buildWorkflowYamlDocument(draft),
        enable_self_healing: legacy,
      };
      const yaml = stringify(document);
      const parsed = yamlCommitInputs(parse(yaml), yaml);
      const settings = applySettingsPatch(draft.settings, parsed.settingsPatch);
      expect(parsed.settingsPatch).not.toHaveProperty("enable_self_healing");
      expect(settings).toEqual(draft.settings);
      expect(
        buildWorkflowSaveRequest({
          ...draft,
          settings,
          workflowDefinitionVersion: 2,
        }),
      ).not.toHaveProperty("enable_self_healing");
    },
  );

  test("null inherits CDP headers and code version; an empty mapping clears CDP headers", () => {
    const current = {
      ...input().settings,
      cdpConnectHeaders: '{"X-Header":"retained"}',
      codeVersion: 1,
    };
    expect(
      applySettingsPatch(current, {
        cdp_connect_headers: null,
        code_version: null,
      }),
    ).toEqual(current);
    expect(
      applySettingsPatch(current, { cdp_connect_headers: {} })
        .cdpConnectHeaders,
    ).toBe("{}");
  });

  test.each([
    { country: "US", subdivision: null, city: null },
    { url: "http://proxy.example.test:8080" },
  ])(
    "round-trips the persisted proxy shape %j during an unrelated YAML edit",
    (proxy) => {
      // API payloads include shapes that the shared visual proxy type does not describe.
      const stored = {
        ...workflow,
        proxy_location: proxy,
      } as unknown as WorkflowApiResponse;
      const draft = {
        ...input(),
        workflow: stored,
        settings: apiWorkflowToSettings(stored),
      };
      const document = buildWorkflowYamlDocument(draft);
      document.description = "Updated description";
      const yaml = stringify(document);
      const parsed = yamlCommitInputs(parse(yaml), yaml);
      const settings = applySettingsPatch(draft.settings, parsed.settingsPatch);
      expect(
        buildWorkflowSaveRequest({
          ...draft,
          settings,
          workflowDefinitionVersion: 2,
        }).proxy_location,
      ).toEqual(proxy);
    },
  );

  test("absent fields preserve live values; replacements do not merge objects", () => {
    const current = {
      ...input().settings,
      codeVersion: 1,
      scriptCacheKey: "custom",
      extraHttpHeaders: '{"old":"value"}',
      errorCodeMapping: { old: "message" },
      model: { model_name: "old", old: true },
    };
    expect(applySettingsPatch(current, {})).toEqual(current);
    const merged = applySettingsPatch(current, {
      code_version: null,
      cache_key: null,
      extra_http_headers: { new: "value" },
      error_code_mapping: { new: "message" },
      model: { model_name: "new" },
      browser_profile_key: "  {{ account }}  ",
      run_with: "ai",
      max_screenshot_scrolls: 0,
    });
    expect(merged).toMatchObject({
      codeVersion: 1,
      scriptCacheKey: "default",
      extraHttpHeaders: '{"new":"value"}',
      errorCodeMapping: { new: "message" },
      model: { model_name: "new" },
      browserProfileKey: "{{ account }}",
      runWith: "agent",
      maxScreenshotScrolls: 0,
    });
    expect(merged.model).toEqual({ model_name: "new" });
    expect(current.extraHttpHeaders).toBe('{"old":"value"}');
    expect(
      applySettingsPatch(current, {
        cache_key: "",
        browser_profile_key: "   ",
        run_with: "code_v2",
      }),
    ).toMatchObject({
      scriptCacheKey: "default",
      browserProfileKey: null,
      runWith: "code",
    });
  });

  test("null follows each field's update semantics", () => {
    const patch = {
      webhook_callback_url: null,
      browser_profile_id: null,
      browser_profile_key: null,
      model: null,
      max_screenshot_scrolls: null,
      max_elapsed_time_minutes: null,
      sequential_key: null,
      totp_verification_url: null,
      totp_identifier: null,
      finally_block_label: null,
      workflow_system_prompt: null,
      error_code_mapping: null,
      retry_policy: null,
      extra_http_headers: null,
      cdp_connect_headers: null,
      mask_secrets: null,
      persist_browser_session: null,
      reuse_browser_session: null,
      pin_saved_session_ip: null,
      run_sequentially: null,
      adaptive_caching: null,
      ai_fallback: null,
      proxy_location: null,
      run_with: null,
    };
    const current = {
      ...input().settings,
      maskSecrets: true,
      persistBrowserSession: true,
      aiFallback: false,
      runWith: "code",
    };
    expect(applySettingsPatch(current, patch)).toEqual({
      ...current,
      webhookCallbackUrl: null,
      browserProfileId: null,
      browserProfileKey: null,
      model: null,
      maxScreenshotScrolls: null,
      maxElapsedTimeMinutes: null,
      sequentialKey: null,
      totpVerificationUrl: null,
      totpIdentifier: null,
      finallyBlockLabel: null,
      workflowSystemPrompt: null,
      errorCodeMapping: null,
      retryPolicy: null,
      extraHttpHeaders: null,
      cdpConnectHeaders: null,
      maskSecrets: true,
      persistBrowserSession: false,
      reuseBrowserSession: false,
      pinSavedSessionIp: false,
      runSequentially: false,
      adaptiveCaching: false,
      generateScriptOnTerminal: false,
      aiFallback: true,
      proxyLocation: null,
      runWith: "agent",
    });
  });

  test.each([
    ["max_screenshot_scrolls", 1001],
    ["max_screenshot_scrolls", -1],
    ["max_screenshot_scrolls", 0.5],
    ["max_elapsed_time_minutes", 481],
    ["max_elapsed_time_minutes", 0],
    ["code_version", 3],
    ["code_version", "1"],
    ["persist_browser_session", "true"],
    ["mask_secrets", 1],
    ["browser_profile_key", {}],
    ["extra_http_headers", []],
    ["extra_http_headers", { a: 1 }],
    ["cdp_connect_headers", "{}"],
    ["error_code_mapping", { a: null }],
    ["run_with", "unknown"],
    ["proxy_location", [{ country: "US" }]],
    ["proxy_location", 123],
    ["model", "model"],
    ["model", {}],
    ["model", { model_name: 1 }],
    ["finally_block_label", false],
    ["totp_identifier", 123],
    ["webhook_callback_url", 123],
  ])("rejects %s=%j with its field name", (field, value) => {
    expect(() => validateSettingsPatch({ [field]: value })).toThrow(
      YamlCommitError,
    );
    expect(() => validateSettingsPatch({ [field]: value })).toThrow(field);
  });

  test("accepts boundary values without clamping and leaves profile syntax to the server", () => {
    expect(
      applySettingsPatch(input().settings, {
        max_screenshot_scrolls: 1000,
        max_elapsed_time_minutes: 480,
        code_version: 2,
        browser_profile_key: "{{ unfinished",
      }),
    ).toMatchObject({
      maxScreenshotScrolls: 1000,
      maxElapsedTimeMinutes: 480,
      codeVersion: 2,
      browserProfileKey: "{{ unfinished",
    });
  });

  test("requires an explicit finally label to target a top-level terminal block", () => {
    const blocks = [
      { label: "first", next_block_label: "last" },
      { label: "last", next_block_label: null },
    ];
    expect(() =>
      resolveFinallyBlockLabel(
        "first",
        { finally_block_label: "first" },
        blocks,
      ),
    ).toThrow("finally_block_label");
    expect(() =>
      resolveFinallyBlockLabel(
        "nested",
        { finally_block_label: "nested" },
        blocks,
      ),
    ).toThrow("finally_block_label");
    expect(
      resolveFinallyBlockLabel("last", { finally_block_label: "last" }, blocks),
    ).toBe("last");
    expect(resolveFinallyBlockLabel("first", {}, blocks)).toBeNull();
    expect(resolveFinallyBlockLabel("last", {}, blocks)).toBe("last");
    expect(
      resolveFinallyBlockLabel(null, { finally_block_label: null }, blocks),
    ).toBeNull();
  });
});

const validRetryPolicy = {
  max_retries: 2,
  delay_seconds: 0,
  webhook_on_retry: "final_only" as const,
  retry_on: [
    {
      status: "failed" as const,
      error_codes: [" spaced ", "spaced", " spaced ", " "],
    },
  ],
};
describe("retry policy validation", () => {
  test.each([
    [{ ...validRetryPolicy, foo: true }, "retry_policy.foo"],
    [
      { ...validRetryPolicy, retry_on: [{ status: "failed", foo: true }] },
      "retry_policy.retry_on[0].foo",
    ],
  ])("rejects unknown retry settings in %j", (policy, field) => {
    expect(() => validateRetryPolicyInput(policy)).toThrow(
      new YamlCommitError(field as string, "unknown setting"),
    );
  });
  test("accepts retry shorthand with backend defaults but rejects explicit null defaults", () => {
    const shorthand = parse("retry_on:\n  - status: failed") as unknown;
    expect(validateRetryPolicyInput(shorthand)).toEqual({
      max_retries: 1,
      delay_seconds: 0,
      webhook_on_retry: "final_only",
      retry_on: [{ status: "failed" }],
    });
    expect(() =>
      validateRetryPolicyInput(
        parse("max_retries: null\nretry_on:\n  - status: failed"),
      ),
    ).toThrow("max_retries must be an integer from 1 to 5");
    expect(() =>
      validateRetryPolicyInput({
        delay_seconds: null,
        retry_on: [{ status: "failed" }],
      }),
    ).toThrow("delay_seconds must be an integer from 0 to 3600");
    expect(() =>
      validateRetryPolicyInput({
        webhook_on_retry: null,
        retry_on: [{ status: "failed" }],
      }),
    ).toThrow("invalid webhook_on_retry");
  });
  test("deduplicates without trimming or dropping valid strings", () => {
    const validated = validateRetryPolicyInput(validRetryPolicy);
    expect(validated?.retry_on[0]?.error_codes).toEqual([
      " spaced ",
      "spaced",
      " ",
    ]);
    expect(normalizeRetryPolicy(validated)).toEqual(validated);
    expect(validateRetryPolicyInput(null)).toBeNull();
  });
  test.each([
    undefined,
    [],
    "policy",
    {},
    { ...validRetryPolicy, max_retries: 0 },
    { ...validRetryPolicy, max_retries: 6 },
    { ...validRetryPolicy, max_retries: 1.5 },
    { ...validRetryPolicy, delay_seconds: -1 },
    { ...validRetryPolicy, delay_seconds: 3601 },
    { ...validRetryPolicy, delay_seconds: "1" },
    { ...validRetryPolicy, webhook_on_retry: "unknown" },
    { ...validRetryPolicy, retry_on: {} },
    { ...validRetryPolicy, retry_on: [] },
    { ...validRetryPolicy, retry_on: [null] },
    { ...validRetryPolicy, retry_on: [{ status: "unknown" }] },
    {
      ...validRetryPolicy,
      retry_on: [{ status: "failed" }, { status: "failed" }],
    },
    {
      ...validRetryPolicy,
      retry_on: [{ status: "failed", error_codes: "code" }],
    },
    { ...validRetryPolicy, retry_on: [{ status: "failed", error_codes: [1] }] },
  ])("rejects malformed policy %j", (value) => {
    expect(() => validateRetryPolicyInput(value)).toThrow("retry_policy");
  });
});

describe("serialized workflow saves", () => {
  test.each([null, "", "{}"])(
    "clears headers with %j and sends explicit nullable definition keys",
    (headers) => {
      const draft = input();
      draft.description = "";
      Object.assign(draft.settings, {
        maskSecrets: true,
        finallyBlockLabel: "cleanup",
        workflowSystemPrompt: "Existing prompt",
        errorCodeMapping: { RETRY: "Existing explanation" },
        retryPolicy: {
          max_retries: 2,
          delay_seconds: 0,
          webhook_on_retry: "disabled",
          retry_on: [{ status: "failed", error_codes: null }],
        },
      });
      draft.settings = applySettingsPatch(draft.settings, {
        retry_policy: null,
        finally_block_label: null,
        workflow_system_prompt: null,
        error_code_mapping: null,
        mask_secrets: null,
      });
      draft.settings.cdpConnectHeaders = headers;
      draft.settings.extraHttpHeaders = headers;
      draft.settings.webhookCallbackUrl = "";
      const request = parse(
        stringify(
          buildWorkflowSaveRequest({ ...draft, workflowDefinitionVersion: 2 }),
        ),
      );
      expect(request).toMatchObject({
        description: null,
        webhook_callback_url: null,
        cdp_connect_headers: {},
        extra_http_headers: {},
        mask_secrets: true,
        workflow_definition: {
          retry_policy: null,
          finally_block_label: null,
          workflow_system_prompt: null,
          error_code_mapping: null,
        },
      });
      expect(buildWorkflowYamlDocument(draft).webhook_callback_url).toBeNull();
    },
  );

  test.each([null, "", "custom"])(
    "saves cache key %j with the existing default",
    (key) => {
      const draft = input();
      draft.settings.scriptCacheKey = key;
      const request = parse(
        stringify(
          buildWorkflowSaveRequest({ ...draft, workflowDefinitionVersion: 2 }),
        ),
      );
      expect(request.cache_key).toBe(key || "default");
    },
  );

  test.each([
    ["agent", null, undefined],
    ["agent", 1, 1],
    ["agent", 2, 2],
    ["code", null, 2],
    ["code", 1, 1],
    ["code", 2, 2],
  ] as const)(
    "saves %s with code version %j as %j",
    (runWith, codeVersion, expected) => {
      const draft = input();
      Object.assign(draft.settings, {
        runWith,
        codeVersion,
        cdpConnectHeaders: '{"Authorization":"********","X-Test":"kept"}',
      });
      const request = parse(
        stringify(
          buildWorkflowSaveRequest({ ...draft, workflowDefinitionVersion: 2 }),
        ),
      );
      expect(request.run_with).toBe(runWith);
      if (expected === undefined)
        expect(request).not.toHaveProperty("code_version");
      else expect(request.code_version).toBe(expected);
      expect(request.cdp_connect_headers).toEqual({
        Authorization: "********",
        "X-Test": "kept",
      });
    },
  );
});

describe("metadata lifecycle", () => {
  afterEach(() => {
    useWorkflowTitleStore.getState().resetTitle();
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
  });

  test("scopes recorded metadata edits to the workflow and proposal identity", () => {
    const titles = useWorkflowTitleStore.getState();
    titles.trackCopilotMetadata("wpid_fixture", "chat:turn:1");
    titles.setTitle("Authored title");
    titles.setDescriptionFromUser("Authored description");
    titles.trackCopilotMetadata("wpid_fixture", "chat:turn:1");
    titles.syncTitleFromWorkflow("Draft title");
    titles.setDescriptionFromWorkflow("Draft description");
    expect(
      useWorkflowTitleStore.getState().copilotMetadataEdits["wpid_fixture"]
        ?.edits,
    ).toEqual({ title: "Authored title", description: "Authored description" });
    titles.trackCopilotMetadata("wpid_other", "chat:turn:1");
    expect(
      useWorkflowTitleStore.getState().copilotMetadataEdits["wpid_other"]
        ?.edits,
    ).toEqual({});
    titles.trackCopilotMetadata("wpid_fixture", "chat:turn:1");
    expect(
      useWorkflowTitleStore.getState().copilotMetadataEdits["wpid_fixture"]
        ?.edits.title,
    ).toBe("Authored title");
    titles.trackCopilotMetadata("wpid_fixture", "chat:turn:2");
    expect(
      useWorkflowTitleStore.getState().copilotMetadataEdits["wpid_fixture"]
        ?.edits,
    ).toEqual({});
  });

  test("a persisted placeholder title replaces the custom title in the next save", () => {
    const store = useWorkflowTitleStore.getState();
    store.initializeTitle("Custom title", workflow.workflow_permanent_id);
    applyYamlCommitMetadata(
      { ...workflow, title: "New Agent" },
      { title: "New Agent" },
      true,
    );
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: "New Agent",
      titleHasBeenGenerated: false,
    });
    expect(
      buildWorkflowSaveRequest({
        ...input(),
        title: useWorkflowTitleStore.getState().title,
        workflowDefinitionVersion: 2,
      }).title,
    ).toBe("New Agent");
  });

  test("canvas reinitialization preserves the draft and explicit restore until the workflow session changes", () => {
    const store = useWorkflowTitleStore.getState();
    store.initializeTitle(workflow.title, workflow.workflow_permanent_id);
    store.initializeDescription(
      workflow.workflow_permanent_id,
      workflow.description,
    );
    store.setTitle("YAML title", { fromYamlCommit: true });
    store.setDescriptionFromUser("YAML description");
    store.initializeTitle(workflow.title, workflow.workflow_permanent_id);
    store.initializeTitle(workflow.title, workflow.workflow_permanent_id);
    store.initializeDescription(
      workflow.workflow_permanent_id,
      workflow.description,
    );
    expect(
      buildWorkflowSaveRequest({
        ...input(),
        workflowDefinitionVersion: 2,
        title: useWorkflowTitleStore.getState().title,
        description: useWorkflowTitleStore.getState().description,
      }),
    ).toMatchObject({ title: "YAML title", description: "YAML description" });
    store.setTitle("Title bar edit");
    store.initializeTitle(workflow.title, workflow.workflow_permanent_id);
    expect(useWorkflowTitleStore.getState().title).toBe("Title bar edit");
    store.setTitle("New Workflow");
    store.setDescriptionFromWorkflow("Restored version description");
    store.initializeTitle(workflow.title, workflow.workflow_permanent_id);
    store.initializeDescription(
      workflow.workflow_permanent_id,
      workflow.description,
    );
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: "New Workflow",
      titleHasBeenGenerated: false,
      description: "Restored version description",
    });
    expect(store.isNewTitle()).toBe(true);
    store.initializeTitle("Next title", "wpid_next");
    store.initializeDescription("wpid_next", "Next workflow");
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: "Next title",
      description: "Next workflow",
    });
    store.resetTitleSession(workflow.workflow_permanent_id);
    store.initializeTitle("Stale next title", "wpid_next");
    expect(useWorkflowTitleStore.getState().title).toBe("Next title");
    store.resetTitleSession("wpid_next");
    store.resetDescriptionSession("wpid_next");
    store.initializeTitle("New Agent", "wpid_next");
    store.initializeDescription("wpid_next", "Reopened workflow");
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: "New Agent",
      titleHasBeenGenerated: false,
      description: "Reopened workflow",
    });
    store.setTitleFromCopilotIfDefault("Copilot title");
    store.initializeTitle("New Agent", "wpid_next");
    store.syncTitleFromWorkflow("New Workflow");
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: "Copilot title",
      titleHasBeenGenerated: true,
    });
    store.setTitleFromGeneration("Generated title");
    store.initializeTitle("New Agent", "wpid_next");
    expect(useWorkflowTitleStore.getState().title).toBe("Generated title");
    store.initializeTitle("Deleted workflow title");
    expect(useWorkflowTitleStore.getState().title).toBe(
      "Deleted workflow title",
    );
  });

  test("hydrates, edits, clears, restores, and resets the live description used by saves", () => {
    const store = useWorkflowTitleStore.getState();
    const savedDescription = () =>
      buildWorkflowSaveRequest({
        ...input(),
        workflowDefinitionVersion: 2,
        description: useWorkflowTitleStore.getState().description,
      }).description;
    store.setDescriptionFromWorkflow(workflow.description);
    expect(savedDescription()).toBe("Loaded description");
    store.setDescriptionFromUser("Local description");
    expect(savedDescription()).toBe("Local description");
    store.setDescriptionFromUser("");
    expect(savedDescription()).toBeNull();
    store.setDescriptionFromWorkflow(workflow.description);
    expect(savedDescription()).toBe("Loaded description");
    store.setDescriptionFromWorkflow(null);
    expect(savedDescription()).toBeNull();
    store.setDescriptionFromUser("Another edit");
    store.resetTitle();
    expect(savedDescription()).toBeNull();
  });
});

describe("YAML persistence callbacks", () => {
  beforeEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
    put.mockReset();
  });

  test.each(["cosmetic", "content"] as const)(
    "settles a reserved save after an attempted %s node update",
    async (change) => {
      const owner = createYamlCommitOwner(workflow.workflow_permanent_id);
      registerEditorOwner(owner);
      const initial = getElements(
        [],
        {
          ...input().settings,
          extraHttpHeaders: '{"X-Test":"same"}',
        },
        true,
      );
      initial.nodes.push(createNode({ id: "url-node" }, "url", "open_page"));
      const client = new QueryClient({
        defaultOptions: { mutations: { retry: false } },
      });
      const queryKey = ["workflow", workflow.workflow_permanent_id];
      client.setQueryData(queryKey, workflow);
      const view = renderHook(
        () => {
          const graph = useWorkflowGraphState(initial.nodes, initial.edges);
          return { ...graph, save: useWorkflowSave() };
        },
        {
          wrapper: ({ children }: { children: ReactNode }) =>
            createElement(QueryClientProvider, { client }, children),
        },
      );
      useWorkflowHasChangesStore.getState().setGetSaveData(() => ({
        ...input(),
        blocks: getWorkflowBlocks(view.result.current.nodes, initial.edges),
        settings: getWorkflowSettings(view.result.current.nodes),
        workflowDefinitionVersion: 2,
      }));
      useWorkflowHasChangesStore.getState().setHasChanges(true);
      let finish: ((response: unknown) => void) | undefined;
      put.mockImplementation(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      );
      let saving!: ReturnType<typeof view.result.current.save.mutateAsync>;
      try {
        act(() => {
          saving = view.result.current.save.mutateAsync(undefined);
        });
        await waitFor(() => expect(finish).toBeDefined());
        const revision = useWorkflowYamlEditorStore.getState().revision;
        act(() => {
          view.result.current.onNodesChange([
            { id: "url-node", type: "position", position: { x: 50, y: 100 } },
            { id: "url-node", type: "select", selected: true },
            {
              id: "url-node",
              type: "dimensions",
              dimensions: { width: 400, height: 300 },
            },
          ]);
          if (change === "content")
            view.result.current.setNodes((nodes) =>
              nodes.map((node) =>
                node.type === "url"
                  ? {
                      ...node,
                      data: {
                        ...node.data,
                        url: "https://example.test/edited",
                      },
                    }
                  : node,
              ),
            );
        });
        expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision);
        expect(
          view.result.current.nodes.find((node) => node.id === "url-node")
            ?.data,
        ).toEqual(initial.nodes.find((node) => node.id === "url-node")?.data);
        await act(async () => {
          finish!({ data: workflow });
          await saving;
        });
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
        expect(client.getQueryState(queryKey)?.isInvalidated).toBe(true);
        expect(
          view.result.current.nodes.find((node) => node.id === "url-node")
            ?.position,
        ).toEqual({ x: 50, y: 100 });
      } finally {
        unregisterEditorOwner(owner);
        view.unmount();
        client.clear();
      }
    },
  );

  test("applies the visual save's server title and description to the next save", async () => {
    const titles = useWorkflowTitleStore.getState();
    titles.initializeTitle("New Agent", workflow.workflow_permanent_id);
    const owner = createYamlCommitOwner(workflow.workflow_permanent_id);
    registerEditorOwner(owner);
    useWorkflowHasChangesStore.getState().setGetSaveData(() => ({
      ...input(),
      title: useWorkflowTitleStore.getState().title,
      description: useWorkflowTitleStore.getState().description,
      workflowDefinitionVersion: 2,
    }));
    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const view = renderHook(() => useWorkflowSave(), {
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(QueryClientProvider, { client }, children),
    });
    put.mockResolvedValue({
      data: {
        ...workflow,
        title: "Generated name",
        description: "Saved description",
      },
    });
    try {
      await act(async () => {
        await view.result.current.mutateAsync(undefined);
      });
      expect(parse(put.mock.calls[0]?.[1]).title).toBe("New Agent");
      expect(useWorkflowTitleStore.getState()).toMatchObject({
        title: "Generated name",
        description: "Saved description",
      });
      titles.initializeTitle("New Agent", workflow.workflow_permanent_id);
      await act(async () => {
        await view.result.current.mutateAsync(undefined);
      });
      expect(parse(put.mock.calls[1]?.[1])).toMatchObject({
        title: "Generated name",
        description: "Saved description",
      });
    } finally {
      unregisterEditorOwner(owner);
      view.unmount();
      client.clear();
    }
  });

  test.each(["revision", "owner"])(
    "does not apply visual save metadata after the %s changes",
    async (change) => {
      const owner = createYamlCommitOwner(workflow.workflow_permanent_id);
      registerEditorOwner(owner);
      useWorkflowTitleStore.getState().setTitle("New Agent");
      useWorkflowHasChangesStore
        .getState()
        .setGetSaveData(() => ({ ...input(), workflowDefinitionVersion: 2 }));
      useWorkflowHasChangesStore.getState().setHasChanges(true);
      const client = new QueryClient({
        defaultOptions: { mutations: { retry: false } },
      });
      const invalidate = vi.spyOn(client, "invalidateQueries");
      const view = renderHook(() => useWorkflowSave(), {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(QueryClientProvider, { client }, children),
      });
      let finish!: (response: unknown) => void;
      put.mockImplementation(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      );
      let saving!: ReturnType<typeof view.result.current.mutateAsync>;
      act(() => {
        saving = view.result.current.mutateAsync(undefined);
      });
      await waitFor(() => expect(put).toHaveBeenCalledOnce());
      act(() => {
        if (change === "revision")
          useWorkflowTitleStore
            .getState()
            .setTitle("Local edit", { fromYamlCommit: true });
        else unregisterEditorOwner(owner);
      });
      await act(async () => {
        finish({
          data: {
            ...workflow,
            title: "Generated name",
            description: "Server description",
          },
        });
        if (change === "revision")
          await expect(saving).rejects.toBeInstanceOf(SaveStaleError);
        else await saving;
      });
      expect(useWorkflowTitleStore.getState().title).toBe(
        change === "revision" ? "Local edit" : "New Agent",
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      if (change === "owner") {
        expect(invalidate).toHaveBeenCalledTimes(3);
        for (const queryKey of [
          ["workflow", workflow.workflow_permanent_id],
          ["workflows"],
          ["block-scripts", workflow.workflow_permanent_id],
        ])
          expect(invalidate).toHaveBeenCalledWith({ queryKey });
      } else {
        expect(invalidate).not.toHaveBeenCalled();
      }
      unregisterEditorOwner(owner);
      view.unmount();
      client.clear();
    },
  );

  test.each([
    ["New Agent", "Generated name", true],
    ["My agent", "My agent", true],
    ["New Agent", "New Agent", false],
  ] as const)(
    "reconciles first-save YAML title %s with server title %s",
    async (yamlTitle, savedTitle, titleHasBeenGenerated) => {
      const titles = useWorkflowTitleStore.getState();
      titles.initializeTitle("New Agent", workflow.workflow_permanent_id);
      const document = stringify({
        title: yamlTitle,
        description: "Draft description",
        workflow_definition: { blocks: [] },
      });
      const { metadataPatch } = yamlCommitInputs(parse(document), document);
      const saveData = {
        ...input(),
        title: yamlTitle,
        workflowDefinitionVersion: 2,
      };
      useWorkflowHasChangesStore.getState().setGetSaveData(() => saveData);
      const owner = createYamlCommitOwner(workflow.workflow_permanent_id);
      const client = new QueryClient({
        defaultOptions: { mutations: { retry: false } },
      });
      const view = renderHook(() => useWorkflowSave(), {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(QueryClientProvider, { client }, children),
      });
      const response = {
        data: { ...workflow, title: savedTitle, description: null },
      };
      put.mockResolvedValue(response);
      try {
        await act(async () => {
          const revision = useWorkflowYamlEditorStore.getState().revision;
          expect(beginYamlCommit(owner)).toBe(true);
          const saved = await persistYamlCommitIfCurrent(
            revision,
            () =>
              view.result.current.mutateAsync({
                ...saveData,
                yamlCommit: { owner, revision },
              }),
            owner,
          );
          expect(saved).toEqual({ response });
          if (!saved) throw new Error("Save was unexpectedly rejected");
          applyYamlCommitMetadata(saved.response!.data, metadataPatch, true);
          finishYamlCommit(owner);
        });
        expect(parse(put.mock.calls[0]?.[1])).toMatchObject({
          title: yamlTitle,
        });
        titles.initializeTitle("New Agent", workflow.workflow_permanent_id);
        titles.initializeTitle("Stale title", workflow.workflow_permanent_id);
        expect(useWorkflowTitleStore.getState()).toMatchObject({
          title: savedTitle,
          titleHasBeenGenerated,
          description: null,
        });
      } finally {
        finishYamlCommit(owner);
        view.unmount();
        client.clear();
      }
    },
  );

  test.each(["My agent", "New Agent"])(
    "apply-only YAML title %s remains the draft title",
    (title) => {
      useWorkflowTitleStore.getState().initializeTitle("Existing title");
      applyYamlCommitMetadata(
        workflow,
        { title, description: "YAML description" },
        false,
      );
      expect(useWorkflowTitleStore.getState()).toMatchObject({
        title,
        titleHasBeenGenerated: title !== "New Agent",
        description: "YAML description",
      });
      expect(put).not.toHaveBeenCalled();
    },
  );

  test.each(["unmount", "revision"] as const)(
    "a save settling after %s cannot mark the editor clean",
    async (invalidation) => {
      const saveData = { ...input(), workflowDefinitionVersion: 2 };
      const changes = useWorkflowHasChangesStore.getState();
      changes.setGetSaveData(() => saveData);
      changes.setHasChanges(true);
      changes.setSaidOkToCodeCacheDeletion(true);
      const owner = createYamlCommitOwner(workflow.workflow_permanent_id);
      const client = new QueryClient({
        defaultOptions: { mutations: { retry: false } },
      });
      const invalidate = vi.spyOn(client, "invalidateQueries");
      const view = renderHook(
        () => {
          useLayoutEffect(() => () => invalidateYamlCommitOwner(owner), []);
          return useWorkflowSave();
        },
        {
          wrapper: ({ children }: { children: ReactNode }) =>
            createElement(QueryClientProvider, { client }, children),
        },
      );
      let resolveSave!: () => void;
      put.mockImplementation(
        () =>
          new Promise<void>((resolve) => {
            resolveSave = resolve;
          }),
      );
      const revision = useWorkflowYamlEditorStore.getState().revision;
      let saving!: ReturnType<typeof persistYamlCommitIfCurrent>;
      act(() => {
        beginYamlCommit(owner);
        saving = persistYamlCommitIfCurrent(
          revision,
          () =>
            view.result.current.mutateAsync({
              ...saveData,
              yamlCommit: { owner, revision },
            }),
          owner,
        ).finally(() => finishYamlCommit(owner));
      });
      await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
      const titleBeforeDisposal = useWorkflowTitleStore.getState().title;
      if (invalidation === "unmount") {
        view.unmount();
        expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(
          true,
        );
        changes.setGetSaveData(() => ({
          ...saveData,
          workflow: { ...workflow, workflow_permanent_id: "wpid_next" },
        }));
        useWorkflowTitleStore.getState().setTitle("Next workflow draft");
      } else {
        act(() => useWorkflowYamlEditorStore.getState().bumpRevision());
      }
      await act(async () => {
        resolveSave();
        expect(await saving).toBe(false);
      });
      expect(useWorkflowHasChangesStore.getState()).toMatchObject({
        hasChanges: true,
        saidOkToCodeCacheDeletion: false,
      });
      if (invalidation === "unmount") {
        expect(invalidate).toHaveBeenCalledTimes(3);
        for (const queryKey of [
          ["workflow", workflow.workflow_permanent_id],
          ["workflows"],
          ["block-scripts", workflow.workflow_permanent_id],
        ])
          expect(invalidate).toHaveBeenCalledWith({ queryKey });
      } else expect(invalidate).not.toHaveBeenCalled();
      expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(
        false,
      );
      if (invalidation === "unmount") {
        expect(useWorkflowTitleStore.getState().title).toBe(
          titleBeforeDisposal,
        );
        useWorkflowTitleStore.getState().setTitle("Next workflow draft");
        expect(useWorkflowTitleStore.getState().title).toBe(
          "Next workflow draft",
        );
        expect(useWorkflowYamlEditorStore.getState().error).toBeNull();
      } else {
        expect(useWorkflowYamlEditorStore.getState().error).toContain(
          "Saved on the server, but local edits changed",
        );
        view.unmount();
      }
      client.clear();
    },
  );

  test.each([false, true])(
    "keeps locked YAML edits in their original workflow (discard=%s)",
    async (discard) => {
      vi.useFakeTimers();
      clearDeferredEdits();
      const initial = "parameters: []\nblocks: []\n";
      const draft = "# unsaved edit\n" + initial;
      const firstId = workflow.workflow_permanent_id;
      const secondId = "wpid_other";
      const openEditor = (workflowId: string) => {
        useWorkflowYamlEditorStore.getState().open(initial);
        return render(
          createElement(WorkflowYamlEditor, { variant: "pane", workflowId }),
        );
      };
      let editor = openEditor(firstId);
      try {
        const cm = EditorView.findFromDOM(
          editor.container.querySelector<HTMLElement>(".cm-content")!,
        )!;
        let reservation!: symbol;
        act(() => {
          cm.dispatch({
            changes: { from: 0, to: cm.state.doc.length, insert: draft },
          });
          reservation = beginCopilotAcceptance()!;
        });
        expect(reservation).not.toBeNull();
        expect(useWorkflowYamlEditorStore.getState().draft).toBe(initial);
        editor.unmount();
        if (discard) clearDeferredEdits(firstId);
        editor = openEditor(secondId);
        await act(async () => {
          finishCopilotAcceptance(reservation);
          await vi.advanceTimersByTimeAsync(300);
        });
        expect(useWorkflowYamlEditorStore.getState().draft).toBe(initial);
        expect(
          EditorView.findFromDOM(
            editor.container.querySelector<HTMLElement>(".cm-content")!,
          )!.state.doc.toString(),
        ).toBe(initial);
        editor.unmount();
        editor = openEditor(firstId);
        await act(async () => vi.advanceTimersByTimeAsync(300));
        expect(useWorkflowYamlEditorStore.getState().draft).toBe(
          discard ? initial : draft,
        );
        expect(deferredEdits.size).toBe(0);
      } finally {
        editor.unmount();
        clearDeferredEdits();
        useWorkflowYamlEditorStore.setState(
          useWorkflowYamlEditorStore.getInitialState(),
        );
        vi.useRealTimers();
      }
    },
  );

  test("saves the latest YAML buffer before its debounce and clears only that draft", async () => {
    vi.useFakeTimers();
    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const owner = createYamlCommitOwner(workflow.workflow_permanent_id);
    const initial = stringify({
      title: "Original title",
      workflow_definition: { blocks: [], parameters: [] },
    });
    const latest = initial.replace("Original title", "Latest typed title");
    useWorkflowHasChangesStore.getState().setGetSaveData(() => ({
      ...input(),
      workflowDefinitionVersion: 2,
    }));
    useWorkflowYamlEditorStore.getState().open(initial);
    const unsubscribe = subscribeToYamlDraftChanges(() => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });
    let finish!: (response: unknown) => void;
    put.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const view = renderHook(() => useWorkflowSave(), {
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(QueryClientProvider, { client }, children),
    });
    useWorkflowYamlEditorStore.getState().registerCommit(async () => {
      const state = useWorkflowYamlEditorStore.getState();
      if (!beginYamlCommit(owner)) return false;
      const { metadataPatch } = yamlCommitInputs(
        parse(state.draft),
        state.draft,
      );
      const saved = await persistYamlCommitIfCurrent(
        state.revision,
        () =>
          view.result.current.mutateAsync({
            ...input(),
            ...metadataPatch,
            workflowDefinitionVersion: 2,
            yamlCommit: { owner, revision: state.revision },
          }),
        owner,
      );
      if (!saved) return false;
      useWorkflowHasChangesStore
        .getState()
        .setHasChanges(false, { fromYamlCommit: true });
      state.close();
      return true;
    });
    const editor = render(
      createElement(WorkflowYamlEditor, {
        variant: "pane",
        workflowId: workflow.workflow_permanent_id,
      }),
    );
    try {
      const content =
        editor.container.querySelector<HTMLElement>(".cm-content")!;
      const cm = EditorView.findFromDOM(content)!;
      let saving!: Promise<boolean>;
      await act(async () => {
        cm.dispatch({
          changes: { from: 0, to: cm.state.doc.length, insert: latest },
        });
        saving = commitYamlDraft(true);
      });
      expect(put).toHaveBeenCalledOnce();
      expect(parse(put.mock.calls[0]?.[1])).toMatchObject({
        title: "Latest typed title",
      });
      expect(useWorkflowYamlEditorStore.getState().draft).toBe(latest);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      expect(isWorkflowYamlDirty(useWorkflowYamlEditorStore.getState())).toBe(
        true,
      );
      await act(async () => {
        finish({ data: { ...workflow, title: "Latest typed title" } });
        expect(await saving).toBe(true);
      });
      editor.unmount();
      expect(useWorkflowYamlEditorStore.getState().flushDraft).toBeNull();
      expect(useWorkflowYamlEditorStore.getState().active).toBe(false);
      expect(isWorkflowYamlDirty(useWorkflowYamlEditorStore.getState())).toBe(
        false,
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(300);
      });
      expect(useWorkflowYamlEditorStore.getState().draft).toBe("");
    } finally {
      unsubscribe();
      editor.unmount();
      view.unmount();
      finishYamlCommit(owner);
      client.clear();
      vi.useRealTimers();
    }
  });

  test.each(["cosmetic", "content"] as const)(
    "tracks a %s graph update and preserves a pending save through passive changes",
    async (change) => {
      const owner = createYamlCommitOwner(workflow.workflow_permanent_id);
      registerEditorOwner(owner);
      const initial = getElements(
        [],
        {
          ...input().settings,
          extraHttpHeaders: '{"X-Test":"same"}',
        },
        true,
      );
      initial.nodes.push(createNode({ id: "url-node" }, "url", "open_page"));
      const client = new QueryClient({
        defaultOptions: { mutations: { retry: false } },
      });
      const queryKey = ["workflow", workflow.workflow_permanent_id];
      client.setQueryData(queryKey, workflow);
      const view = renderHook(
        () => {
          const graph = useWorkflowGraphState(initial.nodes, initial.edges);
          return { ...graph, save: useWorkflowSave() };
        },
        {
          wrapper: ({ children }: { children: ReactNode }) =>
            createElement(QueryClientProvider, { client }, children),
        },
      );
      useWorkflowHasChangesStore.getState().setGetSaveData(() => ({
        ...input(),
        blocks: getWorkflowBlocks(view.result.current.nodes, initial.edges),
        settings: getWorkflowSettings(view.result.current.nodes),
        workflowDefinitionVersion: 2,
      }));
      useWorkflowHasChangesStore.getState().setHasChanges(true);
      let finish: ((response: unknown) => void) | undefined;
      put.mockImplementation(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      );
      let saving!: ReturnType<typeof view.result.current.save.mutateAsync>;
      try {
        const revision = useWorkflowYamlEditorStore.getState().revision;
        act(() => {
          view.result.current.setNodes((nodes) =>
            nodes.map((node) => ({
              ...node,
              position: { x: 50, y: 100 },
              selected: true,
              className: "skyvern-block-highlight",
            })),
          );
          view.result.current.setNodes((nodes) =>
            nodes.map((node) => ({ ...node, className: undefined })),
          );
          view.result.current.setNodes((nodes) =>
            nodes.map((node) =>
              node.type === "start" && node.data.withWorkflowSettings
                ? {
                    ...node,
                    data: {
                      ...node.data,
                      extraHttpHeaders: '{ "X-Test": "same" }',
                    },
                  }
                : node,
            ),
          );
          view.result.current.setEdges((edges) =>
            edges.map((edge) => ({ ...edge, selected: true })),
          );
          view.result.current.onNodesChange([
            {
              id: "url-node",
              type: "dimensions",
              dimensions: { width: 400, height: 300 },
            },
          ]);
          if (change === "content")
            view.result.current.setNodes((nodes) =>
              nodes.map((node) =>
                node.type === "url"
                  ? {
                      ...node,
                      data: {
                        ...node.data,
                        url: "https://example.test/edited",
                      },
                    }
                  : node,
              ),
            );
        });
        expect(
          useWorkflowYamlEditorStore.getState().revision === revision,
        ).toBe(change === "cosmetic");
        expect(
          view.result.current.nodes.find((node) => node.id === "url-node")
            ?.position,
        ).toEqual({ x: 50, y: 100 });
        act(() => {
          saving = view.result.current.save.mutateAsync(undefined);
        });
        await waitFor(() => expect(finish).toBeDefined());
        const saveRevision = useWorkflowYamlEditorStore.getState().revision;
        act(() => {
          view.result.current.onNodesChange([
            { id: "url-node", type: "remove" },
            {
              id: "url-node",
              type: "position",
              position: { x: 75, y: 125 },
            },
          ]);
        });
        expect(useWorkflowYamlEditorStore.getState().revision).toBe(
          saveRevision,
        );
        await act(async () => {
          finish!({ data: workflow });
          await saving;
        });
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
        expect(client.getQueryState(queryKey)?.isInvalidated).toBe(true);
        expect(
          view.result.current.nodes.find((node) => node.id === "url-node")
            ?.position,
        ).toEqual({ x: 75, y: 125 });
      } finally {
        unregisterEditorOwner(owner);
        view.unmount();
        client.clear();
      }
    },
  );

  test("does not apply visual save metadata after the revision changes", async () => {
    const owner = createYamlCommitOwner(workflow.workflow_permanent_id);
    registerEditorOwner(owner);
    useWorkflowTitleStore.getState().setTitle("New Agent");
    useWorkflowHasChangesStore
      .getState()
      .setGetSaveData(() => ({ ...input(), workflowDefinitionVersion: 2 }));
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const view = renderHook(() => useWorkflowSave(), {
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(QueryClientProvider, { client }, children),
    });
    let finish!: (response: unknown) => void;
    put.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    let saving!: ReturnType<typeof view.result.current.mutateAsync>;
    act(() => {
      saving = view.result.current.mutateAsync(undefined);
    });
    await waitFor(() => expect(put).toHaveBeenCalledOnce());
    act(() => {
      useWorkflowTitleStore.setState({ title: "Local edit" });
    });
    await act(async () => {
      finish({
        data: {
          ...workflow,
          title: "Generated name",
          description: "Server description",
        },
      });
      await expect(saving).rejects.toBeInstanceOf(SaveStaleError);
    });
    expect(useWorkflowTitleStore.getState().title).toBe("Local edit");
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    expect(invalidate).not.toHaveBeenCalled();
    unregisterEditorOwner(owner);
    view.unmount();
    client.clear();
  });
});

describe("regular save transactions", () => {
  beforeEach(() => {
    vi.mocked(toast).mockClear();
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
    useWorkflowParametersStore.setState(
      useWorkflowParametersStore.getInitialState(),
    );
    put.mockReset();
  });
  afterEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
  });
  function mountSave() {
    const owner = createYamlCommitOwner(workflow.workflow_permanent_id);
    registerEditorOwner(owner);
    useWorkflowHasChangesStore
      .getState()
      .setGetSaveData(() => ({ ...input(), workflowDefinitionVersion: 2 }));
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const view = renderHook(() => useWorkflowSave(), {
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(QueryClientProvider, { client }, children),
    });
    return { owner, view, client };
  }
  test("legacy save refuses a Copilot lock without an unhandled rejection", async () => {
    const { view, client } = mountSave();
    client.setDefaultOptions({
      queries: { staleTime: Infinity, retry: false },
    });
    client.setQueryData(["globalWorkflows"], []);
    client.setQueryData(["workflow", workflow.workflow_permanent_id], workflow);
    expect(beginCopilotAcceptance()).not.toBeNull();
    const unhandled = vi.fn();
    process.on("unhandledRejection", unhandled);
    const control = render(
      createElement(
        QueryClientProvider,
        { client },
        createElement(
          MemoryRouter,
          null,
          createElement(
            WorkflowPermanentIdContext.Provider,
            { value: workflow.workflow_permanent_id },
            createElement(ReactFlowProvider, null, createElement(SaveButton)),
          ),
        ),
      ),
    );
    try {
      fireEvent.click(control.getByRole("button"));
      await waitFor(() =>
        expect(toast).toHaveBeenCalledWith({
          title: "Wait for the Copilot change to finish",
          variant: "destructive",
        }),
      );
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(unhandled).not.toHaveBeenCalled();
      expect(put).not.toHaveBeenCalled();
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    } finally {
      process.off("unhandledRejection", unhandled);
      control.unmount();
      view.unmount();
      client.clear();
    }
  });
  test("defers a recording import until a pending PUT settles, then keeps it dirty", async () => {
    const { view, client } = mountSave();
    const doLayout = vi.fn();
    const recording = renderHook(() => {
      const [nodes, setNodes] = useState<AppNode[]>([]);
      useApplyRecordedBlocks({
        enabled: true,
        nodes,
        edges: [],
        doLayout: (nextNodes, nextEdges) => {
          doLayout(nextNodes, nextEdges);
          setNodes(nextNodes);
        },
      });
      return nodes;
    });
    let resolvePut!: (value: unknown) => void;
    put.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePut = resolve;
        }),
    );
    let saving!: Promise<unknown>;
    act(() => {
      saving = view.result.current.mutateAsync(undefined);
    });
    await waitFor(() => expect(put).toHaveBeenCalledOnce());
    act(() => {
      useRecordedBlocksStore.getState().setRecordedBlocks(
        {
          blocks: [
            {
              block_type: "goto_url",
              label: "imported",
              url: "https://example.test",
              output_parameter: {
                parameter_type: "output",
                key: "imported_output",
                description: null,
                output_parameter_id: "op_fixture",
                workflow_id: workflow.workflow_id,
                created_at: workflow.created_at,
                modified_at: workflow.modified_at,
                deleted_at: null,
              },
              continue_on_failure: false,
              model: null,
            },
          ],
          parameters: [
            {
              key: "recorded_input",
              parameter_type: "credential",
              credential_id: "cred_fixture",
            },
          ],
        },
        { previous: null, next: null, connectingEdgeType: "default" },
        useWorkflowYamlEditorStore.getState().editorOwner!,
      );
    });
    expect.soft(doLayout).not.toHaveBeenCalled();
    expect.soft(useRecordedBlocksStore.getState().blocks).not.toBeNull();
    expect.soft(useWorkflowParametersStore.getState().parameters).toEqual([]);
    await act(async () => {
      resolvePut({ data: workflow });
      await saving;
    });
    expect(doLayout).toHaveBeenCalledOnce();
    expect(
      recording.result.current.some((node) => node.data.label === "imported"),
    ).toBe(true);
    expect(useWorkflowParametersStore.getState().parameters).toEqual([
      expect.objectContaining({ credentialId: "cred_fixture" }),
    ]);
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    expect(useRecordedBlocksStore.getState().blocks).toBeNull();
    recording.unmount();
    view.unmount();
    client.clear();
  });
  test.each([
    ["yaml", "A YAML commit is in progress"],
    ["save", "A save is in progress"],
    ["copilot", "Wait for the Copilot change to finish"],
  ])("refuses a save during %s", async (lock, message) => {
    const { owner, view, client } = mountSave();
    if (lock === "yaml") beginYamlCommit(owner);
    else if (lock === "save") beginSaveTransaction(owner);
    else beginCopilotAcceptance();
    await act(async () => {
      await expect(view.result.current.mutateAsync(undefined)).rejects.toEqual(
        new SaveRefusedError(message),
      );
    });
    expect(put).not.toHaveBeenCalled();
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    view.unmount();
    client.clear();
  });
  test.each(["resolve", "reject"] as const)(
    "keeps an unregistered owner's save locked until PUT %s",
    async (outcome) => {
      const { owner, view, client } = mountSave();
      const invalidate = vi.spyOn(client, "invalidateQueries");
      let resolve!: (value: unknown) => void;
      let reject!: (error: Error) => void;
      put.mockImplementation(
        () =>
          new Promise((res, rej) => {
            resolve = res;
            reject = rej;
          }),
      );
      let saving!: Promise<unknown>;
      act(() => {
        saving = view.result.current
          .mutateAsync(undefined)
          .catch((error) => error);
      });
      await waitFor(() => expect(put).toHaveBeenCalledOnce());
      const nextOwner = createYamlCommitOwner("wpid_next");
      let lockedWhilePending = false;
      let nextSaveStarted = false;
      act(() => {
        unregisterEditorOwner(owner);
        finishYamlCommit(owner);
        useWorkflowYamlEditorStore.getState().setCommitInProgress(false);
        registerEditorOwner(nextOwner);
        expect(
          useWorkflowYamlEditorStore.getState().pendingSaves[
            workflow.workflow_permanent_id
          ]?.owner,
        ).toBe(owner);
        lockedWhilePending =
          useWorkflowYamlEditorStore.getState().commitOwner === owner &&
          useWorkflowYamlEditorStore.getState().commitInProgress;
        nextSaveStarted = beginSaveTransaction(nextOwner);
        if (nextSaveStarted) finishSaveTransaction(nextOwner);
        useWorkflowTitleStore.setState({ title: "Next workflow draft" });
      });
      const response = { data: { ...workflow, title: "Saved title" } };
      const failure = new Error("PUT failed");
      let result: unknown;
      await act(async () => {
        if (outcome === "resolve") resolve(response);
        else reject(failure);
        result = await saving;
      });
      expect(lockedWhilePending).toBe(false);
      expect(nextSaveStarted).toBe(true);
      expect(
        useWorkflowYamlEditorStore.getState().pendingSaves[
          workflow.workflow_permanent_id
        ]?.owner,
      ).toBe(outcome === "reject" ? owner : undefined);
      expect(result).toBe(outcome === "resolve" ? response : failure);
      expect(useWorkflowTitleStore.getState().title).toBe(
        "Next workflow draft",
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        commitInProgress: false,
        commitOwner: null,
        lockKind: null,
        error: null,
      });
      expect(invalidate).toHaveBeenCalledTimes(3);
      for (const queryKey of [
        ["workflow", workflow.workflow_permanent_id],
        ["workflows"],
        ["block-scripts", workflow.workflow_permanent_id],
      ])
        expect(invalidate).toHaveBeenCalledWith({ queryKey });
      expect(toast).not.toHaveBeenCalledWith(
        expect.objectContaining({ title: new SaveStaleError().message }),
      );
      act(() => {
        expect(beginSaveTransaction(nextOwner)).toBe(true);
        finishSaveTransaction(nextOwner);
      });
      view.unmount();
      client.clear();
    },
  );

  test("passive canvas callback refreshes preserve a pending save, but a block edit invalidates it", async () => {
    const { view, client } = mountSave();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const elements = getElements(
      [
        {
          block_type: "code",
          label: "block",
          code: "return 0",
          parameters: [],
          error_code_mapping: null,
          continue_on_failure: false,
          model: null,
          output_parameter: {
            parameter_type: "output",
            key: "block_output",
            description: null,
            output_parameter_id: "op_fixture",
            workflow_id: workflow.workflow_id,
            created_at: workflow.created_at,
            modified_at: workflow.modified_at,
            deleted_at: null,
          },
        },
      ],
      input().settings,
      true,
    );
    let nodes = elements.nodes;
    const block = nodes.find((node) => node.type === "codeBlock")!;
    const registerSaveData = () =>
      useWorkflowHasChangesStore.getState().setGetSaveData(() => ({
        ...input(),
        workflowDefinitionVersion: 2,
        blocks: getWorkflowBlocks(nodes, elements.edges),
      }));
    act(registerSaveData);
    let resolve!: (value: unknown) => void;
    put.mockImplementation(
      () =>
        new Promise((res) => {
          resolve = res;
        }),
    );
    for (const editBlock of [false, true]) {
      let saving!: Promise<unknown>;
      act(() => {
        saving = view.result.current
          .mutateAsync(undefined)
          .catch((error) => error);
      });
      await waitFor(() => expect(put).toHaveBeenCalledTimes(editBlock ? 2 : 1));
      const revision = useWorkflowYamlEditorStore.getState().revision;
      act(() => {
        const changes: NodeChange<AppNode>[] = [
          { type: "select", id: block.id, selected: true },
          {
            type: "dimensions",
            id: block.id,
            dimensions: { width: 100, height: 50 },
          },
          {
            type: "position",
            id: block.id,
            position: { x: 10, y: 20 },
            dragging: true,
          },
        ];
        for (const change of changes) {
          nodes = applyNodeChanges([change], nodes);
          registerSaveData();
        }
        if (editBlock) {
          nodes = nodes.map((node) =>
            node.id === block.id
              ? ({
                  ...node,
                  data: { ...node.data, code: "return 1" },
                } as AppNode)
              : node,
          );
          registerSaveData();
          // An internal edit can bypass the user-write lock, but must still invalidate the save.
          useWorkflowHasChangesStore
            .getState()
            .setHasChanges(true, { fromYamlCommit: true });
        }
      });
      const changedRevision = useWorkflowYamlEditorStore.getState().revision;
      const response = { data: workflow };
      let result: unknown;
      await act(async () => {
        resolve(response);
        result = await saving;
      });
      if (editBlock) {
        expect(result).toBeInstanceOf(SaveStaleError);
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
        expect(invalidate).not.toHaveBeenCalled();
      } else {
        expect(changedRevision).toBe(revision);
        expect(result).toBe(response);
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
        expect(invalidate).toHaveBeenCalledWith({
          queryKey: ["workflow", workflow.workflow_permanent_id],
        });
      }
      invalidate.mockClear();
    }
    view.unmount();
    client.clear();
  });
  test.each([false, true])(
    "holds parameter edits and checks stale revision %s",
    async (stale) => {
      const { view, client } = mountSave();
      const original = [
        {
          key: "input",
          parameterType: "context" as const,
          sourceParameterKey: "source",
        },
      ];
      useWorkflowParametersStore.getState().setParameters(original);
      useWorkflowTitleStore.getState().setTitle("Local title");
      let resolve!: (value: unknown) => void;
      put.mockImplementation(
        () =>
          new Promise((r) => {
            resolve = r;
          }),
      );
      let saving!: Promise<unknown>;
      act(() => {
        saving = view.result.current
          .mutateAsync(undefined)
          .catch((error: unknown) => error);
      });
      await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
      useWorkflowParametersStore.getState().setParameters([]);
      expect(useWorkflowParametersStore.getState().parameters).toEqual(
        original,
      );
      if (stale) useWorkflowYamlEditorStore.getState().bumpRevision();
      vi.mocked(toast).mockClear();
      await act(async () => {
        resolve({ data: { ...workflow, title: "Saved title" } });
        const result = await saving;
        if (stale) expect(result).toBeInstanceOf(SaveStaleError);
      });
      expect(toast).toHaveBeenCalledOnce();
      if (stale) {
        expect(toast).toHaveBeenCalledWith({
          title: new SaveStaleError().message,
          variant: "destructive",
        });
        expect(useWorkflowYamlEditorStore.getState().error).toBe(
          new SaveStaleError().message,
        );
      }
      expect(useWorkflowHasChangesStore.getState()).toMatchObject({
        hasChanges: stale,
        saveGeneration: 1,
      });
      expect(useWorkflowTitleStore.getState().title).toBe(
        stale ? "Local title" : "Saved title",
      );
      expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(
        false,
      );
      view.unmount();
      client.clear();
    },
  );
  test("cache confirmation retries acquire a new transaction", async () => {
    const { view, client } = mountSave();
    put.mockRejectedValueOnce({
      response: { data: { detail: "No confirmation for code cache deletion" } },
    });
    await act(async () => {
      await view.result.current.mutateAsync(undefined).catch(() => {});
    });
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(false);
    expect(
      useWorkflowHasChangesStore.getState().showConfirmCodeCacheDeletion,
    ).toBe(true);
    put.mockImplementationOnce(async () => {
      expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(true);
      return { data: workflow };
    });
    await act(async () => {
      expect(
        await confirmCodeCacheDeletion(async () => {
          await view.result.current.mutateAsync(undefined);
        }),
      ).toBe(true);
    });
    expect(put.mock.calls[1]?.[2].params.delete_code_cache_is_ok).toBe("true");
    expect(
      useWorkflowHasChangesStore.getState().saidOkToCodeCacheDeletion,
    ).toBe(false);
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(false);
    view.unmount();
    client.clear();
  });
  test.each(["refused", "stale"])(
    "cache confirmation clears authorization on a %s save",
    async (outcome) => {
      const { owner, view, client } = mountSave();
      useWorkflowHasChangesStore
        .getState()
        .setShowConfirmCodeCacheDeletion(true);
      if (outcome === "refused") beginYamlCommit(owner);
      put.mockImplementationOnce(async () => {
        useWorkflowYamlEditorStore.getState().bumpRevision();
        return { data: workflow };
      });
      await act(async () => {
        expect(
          await confirmCodeCacheDeletion(async () => {
            await view.result.current.mutateAsync(undefined);
          }),
        ).toBe(false);
      });
      expect(useWorkflowHasChangesStore.getState()).toMatchObject({
        saidOkToCodeCacheDeletion: false,
        showConfirmCodeCacheDeletion: false,
        hasChanges: true,
      });
      expect(toast).toHaveBeenCalledOnce();
      expect(toast).toHaveBeenCalledWith({
        title:
          outcome === "refused"
            ? "A YAML commit is in progress"
            : new SaveStaleError().message,
        variant: "destructive",
      });
      finishYamlCommit(owner);
      view.unmount();
      client.clear();
    },
  );
});
