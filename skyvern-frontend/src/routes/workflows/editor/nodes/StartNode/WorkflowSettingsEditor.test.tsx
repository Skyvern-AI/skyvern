// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { RetryPolicyEditor } from "./RetryPolicyEditor";
import type { WorkflowStartNodeData } from "./types";

const mockUpdateNodeData = vi.fn();
const startNodeData: WorkflowStartNodeData = {
  withWorkflowSettings: true,
  webhookCallbackUrl: "",
  proxyLocation: null,
  totpVerificationUrl: null,
  totpIdentifier: null,
  adaptiveCaching: false,
  generateScriptOnTerminal: false,
  persistBrowserSession: true,
  reuseBrowserSession: false,
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
  maskSecrets: false,
  runSequentially: false,
  sequentialKey: null,
  finallyBlockLabel: null,
  workflowSystemPrompt: null,
  errorCodeMapping: null,
  retryPolicy: null,
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
    useNodes: () => [{ id: "start", type: "start", data: nodeData }],
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

vi.mock("@/routes/workflows/hooks/useWorkflowQuery", async (importOriginal) => {
  const actual =
    await importOriginal<
      typeof import("@/routes/workflows/hooks/useWorkflowQuery")
    >();
  return { ...actual, useWorkflowQuery: vi.fn(actual.useWorkflowQuery) };
});

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

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: ({ content }: { content: string }) => <span>{content}</span>,
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

import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
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
  vi.mocked(useWorkflowQuery).mockReset();
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

describe("WorkflowSettingsEditor browser session reuse", () => {
  test("enabling reuse also stores profile persistence", () => {
    renderSettings({
      persistBrowserSession: false,
      reuseBrowserSession: false,
    });

    const label = screen.getByText("Reuse browser session");
    const toggle = label.parentElement?.parentElement?.querySelector(
      'button[role="switch"]',
    );
    expect(toggle).not.toBeNull();
    fireEvent.click(toggle!);

    expect(mockUpdateNodeData).toHaveBeenCalledWith("start", {
      reuseBrowserSession: true,
      persistBrowserSession: true,
    });
  });

  test("disabling reuse keeps profile persistence enabled", () => {
    renderSettings({
      persistBrowserSession: true,
      reuseBrowserSession: true,
    });

    const label = screen.getByText("Reuse browser session");
    const toggle = label.parentElement?.parentElement?.querySelector(
      'button[role="switch"]',
    );
    expect(toggle).not.toBeNull();
    fireEvent.click(toggle!);

    expect(mockUpdateNodeData).toHaveBeenCalledWith("start", {
      reuseBrowserSession: false,
      persistBrowserSession: true,
    });
  });

  test("shows profile persistence as enabled and locked while reuse is on", () => {
    renderSettings({
      persistBrowserSession: true,
      reuseBrowserSession: true,
    });

    const label = screen.getByText("Save & reuse browser profile");
    const toggle = label.parentElement?.querySelector('button[role="switch"]');
    expect(toggle?.getAttribute("data-state")).toBe("checked");
    expect(toggle?.hasAttribute("disabled")).toBe(true);
  });
});

describe("WorkflowSettingsEditor mask secrets setting", () => {
  test("renders the pinned tooltip and updates maskSecrets", () => {
    renderSettings();

    const label = screen.getByText("Mask Secrets");
    expect(
      screen.getByText(
        "Mask secret values in this workflow's runs. Secrets are hidden while they are typed (screenshots, recordings, live browser view) and redacted from stored artifacts, network logs, and LLM prompts. Turning this on can make debugging harder because secret values are hidden.",
      ),
    ).toBeDefined();

    const toggle = label.parentElement?.querySelector('button[role="switch"]');
    expect(toggle).not.toBeNull();
    fireEvent.click(toggle!);

    expect(mockUpdateNodeData).toHaveBeenCalledWith("start", {
      maskSecrets: true,
    });
  });
});

describe("WorkflowSettingsEditor code block AI fallback", () => {
  test("has no per-workflow switch on a copilot-authored workflow in Studio", () => {
    vi.mocked(useWorkflowQuery).mockReturnValue({
      data: { title: "Copilot workflow", copilot_authored: true },
    } as unknown as ReturnType<typeof useWorkflowQuery>);
    renderSettings();

    expect(screen.queryByText("Code Block Self-Healing")).toBeNull();
  });
});

describe("WorkflowSettingsEditor retry policy", () => {
  test("enables, edits, and disables automatic retry", () => {
    let view = renderSettings({
      retryPolicy: null,
      errorCodeMapping: { E_RETRY: "Retryable failure" },
    });

    function applyUpdate(retryPolicy: WorkflowStartNodeData["retryPolicy"]) {
      expect(mockUpdateNodeData).toHaveBeenCalledExactlyOnceWith("start", {
        retryPolicy,
      });
      nodeData = { ...nodeData, ...mockUpdateNodeData.mock.calls[0]![1] };
      mockUpdateNodeData.mockClear();
      view.unmount();
      view = renderSettings(nodeData);
    }

    fireEvent.click(screen.getByRole("switch", { name: "Automatic retry" }));
    const defaultPolicy: NonNullable<WorkflowStartNodeData["retryPolicy"]> = {
      max_retries: 1,
      delay_seconds: 0,
      webhook_on_retry: "final_only",
      retry_on: [{ status: "failed" }],
    };
    applyUpdate(defaultPolicy);

    const maxRetries = screen.getByLabelText(
      "Maximum retries",
    ) as HTMLInputElement;
    fireEvent.change(maxRetries, { target: { value: "" } });
    expect(mockUpdateNodeData).not.toHaveBeenCalled();
    expect(maxRetries.value).toBe("");
    fireEvent.change(maxRetries, { target: { value: "9" } });
    applyUpdate({ ...defaultPolicy, max_retries: 5 });

    fireEvent.change(screen.getByLabelText("Maximum retries"), {
      target: { value: "" },
    });
    fireEvent.blur(screen.getByLabelText("Maximum retries"));
    applyUpdate(defaultPolicy);

    fireEvent.change(screen.getByLabelText("Maximum retries"), {
      target: { value: "2" },
    });
    applyUpdate({ ...defaultPolicy, max_retries: 2 });

    fireEvent.change(
      screen.getByLabelText("Delay between attempts (seconds)"),
      {
        target: { value: "5" },
      },
    );
    const configuredPolicy = {
      ...defaultPolicy,
      max_retries: 2,
      delay_seconds: 5,
    };
    applyUpdate(configuredPolicy);

    fireEvent.change(
      screen.getByLabelText("Delay between attempts (seconds)"),
      {
        target: { value: "" },
      },
    );
    fireEvent.blur(screen.getByLabelText("Delay between attempts (seconds)"));
    applyUpdate({ ...configuredPolicy, delay_seconds: 0 });
    fireEvent.change(
      screen.getByLabelText("Delay between attempts (seconds)"),
      {
        target: { value: "5" },
      },
    );
    applyUpdate(configuredPolicy);

    fireEvent.click(
      screen.getByRole("button", { name: "Enter a custom value" }),
    );
    fireEvent.change(screen.getByLabelText("Error codes"), {
      target: { value: "E_RETRY" },
    });
    fireEvent.keyDown(screen.getByLabelText("Error codes"), { key: "Enter" });
    applyUpdate({
      ...configuredPolicy,
      retry_on: [{ status: "failed", error_codes: ["E_RETRY"] }],
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Enter a custom value" }),
    );
    fireEvent.change(screen.getByLabelText("Error codes"), {
      target: { value: "E_CUSTOM" },
    });
    fireEvent.keyDown(screen.getByLabelText("Error codes"), { key: "Enter" });
    applyUpdate({
      ...configuredPolicy,
      retry_on: [{ status: "failed", error_codes: ["E_RETRY", "E_CUSTOM"] }],
    });

    fireEvent.click(screen.getByRole("switch", { name: "Automatic retry" }));
    applyUpdate(null);
    expect(screen.queryByLabelText("Maximum retries")).toBeNull();
  });
});

test("changing a retry status preserves the custom error-code draft", () => {
  const policy = {
    max_retries: 1,
    delay_seconds: 0,
    webhook_on_retry: "final_only" as const,
    retry_on: [{ status: "failed" as const }],
  };
  const view = (status: "failed" | "terminated") => (
    <RetryPolicyEditor
      value={{ ...policy, retry_on: [{ status }] }}
      onChange={() => {}}
      knownErrorCodes={[]}
      readOnly={false}
    />
  );
  const { rerender } = render(view("failed"));
  fireEvent.click(screen.getByRole("button", { name: "Enter a custom value" }));
  fireEvent.change(screen.getByLabelText("Error codes"), {
    target: { value: "E_DRAFT" },
  });
  rerender(view("terminated"));
  expect((screen.getByLabelText("Error codes") as HTMLInputElement).value).toBe(
    "E_DRAFT",
  );
});
