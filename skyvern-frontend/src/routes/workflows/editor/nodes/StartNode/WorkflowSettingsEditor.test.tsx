// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { WorkflowStartNodeData } from "./types";

const mockUpdateNodeData = vi.fn();
const startNodeData: WorkflowStartNodeData = {
  withWorkflowSettings: true,
  webhookCallbackUrl: "",
  proxyLocation: null,
  persistBrowserSession: true,
  pinSavedSessionIp: false,
  browserProfileId: null,
  browserProfileKey: null,
  model: null,
  maxScreenshotScrolls: null,
  maxElapsedTimeMinutes: null,
  extraHttpHeaders: null,
  cdpConnectHeaders: null,
  editable: true,
  runWith: "agent",
  codeVersion: null,
  scriptCacheKey: null,
  aiFallback: true,
  enableSelfHealing: false,
  runSequentially: false,
  sequentialKey: null,
  finallyBlockLabel: null,
  workflowSystemPrompt: null,
  errorCodeMapping: null,
  label: "__start_block__",
  showCode: false,
};

let nodeData = startNodeData;

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useNodesData: (id: string) =>
      id === "start"
        ? {
            id,
            type: "start",
            data: nodeData,
          }
        : null,
    useNodes: () => [],
    useEdges: () => [],
    useReactFlow: () => ({
      updateNodeData: mockUpdateNodeData,
    }),
  };
});

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({
    value,
    onChange,
    placeholder,
    "data-testid": dataTestId,
  }: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
    "data-testid"?: string;
  }) => (
    <textarea
      data-testid={dataTestId}
      placeholder={placeholder}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/components/ModelSelector", () => ({
  ModelSelector: () => <div data-testid="model-selector" />,
}));

vi.mock("@/components/ProxySelector", () => ({
  ProxySelector: () => <div data-testid="proxy-selector" />,
}));

vi.mock("@/routes/workflows/components/BrowserProfileSelector", () => ({
  BrowserProfileSelector: () => <div data-testid="browser-profile-selector" />,
}));

vi.mock("@/components/KeyValueInput", () => ({
  KeyValueInput: () => <div data-testid="key-value-input" />,
}));

vi.mock("@/components/TestWebhookDialog", () => ({
  TestWebhookDialog: () => <div data-testid="test-webhook-dialog" />,
}));

vi.mock("@/routes/workflows/hooks/useResetProfileMutation", () => ({
  useResetProfileMutation: () => ({
    mutate: vi.fn(),
    isPending: false,
  }),
}));

vi.mock("@/components/ui/select", () => ({
  Select: ({
    children,
    onValueChange,
    value,
  }: {
    children: ReactNode;
    onValueChange: (value: string) => void;
    value: string;
  }) => (
    <div data-select-value={value}>
      {children}
      <button
        type="button"
        data-testid="select-credential_id"
        onClick={() => onValueChange("credential_id")}
      >
        credential_id
      </button>
    </div>
  ),
  SelectContent: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  SelectItem: ({ children, value }: { children: ReactNode; value: string }) => (
    <div data-value={value}>{children}</div>
  ),
  SelectTrigger: ({
    children,
    "data-testid": dataTestId,
  }: {
    children: ReactNode;
    "data-testid"?: string;
  }) => <button data-testid={dataTestId}>{children}</button>,
  SelectValue: ({ placeholder }: { placeholder: string }) => (
    <span>{placeholder}</span>
  ),
}));

import { WorkflowSettingsEditor } from "./WorkflowSettingsEditor";

function renderSettings(overrides: Partial<WorkflowStartNodeData> = {}) {
  nodeData = {
    ...startNodeData,
    ...overrides,
  };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkflowSettingsEditor blockId="start" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  nodeData = startNodeData;
  mockUpdateNodeData.mockClear();
});

afterEach(() => {
  cleanup();
});

describe("WorkflowSettingsEditor browser profile key field", () => {
  test("uses a templated key textarea without a dropdown or code toggle", () => {
    renderSettings();

    expect(screen.getByTestId("browser-profile-key-template")).toBeDefined();
    expect(screen.queryByTestId("browser-profile-key-input-select")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Enter a custom value" }),
    ).toBeNull();
    expect(screen.getByText(/Use \+ to insert an input/)).toBeDefined();
  });

  test("updates browserProfileKey from the textarea", () => {
    renderSettings();

    fireEvent.change(screen.getByTestId("browser-profile-key-template"), {
      target: { value: "{{ credential_id }}" },
    });

    expect(mockUpdateNodeData).toHaveBeenCalledWith("start", {
      browserProfileKey: "{{ credential_id }}",
    });
  });

  test("clears browserProfileKey when the textarea is emptied", () => {
    renderSettings({ browserProfileKey: "{{ credential_id }}" });

    fireEvent.change(screen.getByTestId("browser-profile-key-template"), {
      target: { value: "" },
    });

    expect(mockUpdateNodeData).toHaveBeenCalledWith("start", {
      browserProfileKey: null,
    });
  });

  test("shows stored raw keys in the textarea", () => {
    renderSettings({ browserProfileKey: "tenant-a" });

    expect(
      (
        screen.getByTestId(
          "browser-profile-key-template",
        ) as HTMLTextAreaElement
      ).value,
    ).toBe("tenant-a");
  });

  test("shows stored templated keys in the textarea", () => {
    renderSettings({ browserProfileKey: "{{ credential_id }}" });

    expect(
      (
        screen.getByTestId(
          "browser-profile-key-template",
        ) as HTMLTextAreaElement
      ).value,
    ).toBe("{{ credential_id }}");
  });
});
