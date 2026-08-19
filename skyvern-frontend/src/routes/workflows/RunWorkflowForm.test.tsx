// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode, SelectHTMLAttributes } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type * as ReactRouterDom from "react-router-dom";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  test,
  vi,
} from "vitest";
import posthog from "posthog-js";

import { ProxyLocation } from "@/api/types";
import {
  getRecoveryGuidanceRetryNavigation,
  type RecoveryGuidanceTelemetryContext,
} from "@/util/onboarding/recoveryGuidanceTelemetry";

type RunRequestPayload = {
  reuse_browser_session: boolean | null;
  [key: string]: unknown;
};

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  post: vi.fn<
    (
      url: string,
      body: RunRequestPayload,
    ) => Promise<{ data: { workflow_run_id: string } }>
  >(),
  workflow: {
    title: "Test workflow",
    workflow_definition: {
      blocks: [],
      parameters: [],
    },
    run_with: "agent",
    ai_fallback: true,
    cache_key: "default",
    browser_profile_key: null,
  },
}));

vi.mock("posthog-js", () => ({
  default: { capture: vi.fn() },
}));

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof ReactRouterDom>("react-router-dom");
  return {
    ...actual,
    useNavigate: () => mocks.navigate,
  };
});

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({ post: mocks.post }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));
vi.mock("@/hooks/useApiCredential", () => ({
  useApiCredential: () => null,
}));
vi.mock("@/routes/workflows/hooks/useWorkflowQuery", () => ({
  useWorkflowQuery: () => ({ data: mocks.workflow }),
}));
vi.mock("@/routes/workflows/hooks/useCredentialsQuery", () => ({
  useCredentialsQuery: () => ({ data: [] }),
}));
vi.mock("@/routes/workflows/hooks/useBlockScriptsQuery", () => ({
  useBlockScriptsQuery: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => false,
}));
vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: (flag: string) => flag === "browser_memory_v1",
}));
vi.mock("posthog-js/react", () => ({
  useFeatureFlagVariantKey: () => undefined,
}));
vi.mock("@/store/onboarding/useOnboardingState", () => ({
  useOnboardingStateOptional: () => null,
}));
vi.mock("@/components/CopyApiCommandDropdown", () => ({
  CopyApiCommandDropdown: () => null,
}));
vi.mock("@/components/ProxySelector", () => ({
  ProxySelector: () => <div data-testid="proxy-selector" />,
}));
vi.mock("@/routes/workflows/components/BrowserProfileSelector", () => ({
  BrowserProfileSelector: () => <div data-testid="browser-profile-selector" />,
}));
vi.mock("@/routes/workflows/components/BrowserProfileControl", () => ({
  BrowserProfileControl: () => <div data-testid="browser-profile-control" />,
}));
vi.mock("@/components/KeyValueInput", () => ({
  KeyValueInput: () => <div data-testid="key-value-input" />,
}));
vi.mock("@/components/TestWebhookDialog", () => ({
  TestWebhookDialog: () => null,
}));
vi.mock("@/components/ui/accordion", () => ({
  Accordion: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  AccordionContent: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  AccordionItem: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  AccordionTrigger: ({ children }: { children: ReactNode }) => (
    <button type="button">{children}</button>
  ),
}));
vi.mock("@/components/ui/select", () => ({
  Select: ({
    children,
    onValueChange,
    ...props
  }: SelectHTMLAttributes<HTMLSelectElement> & {
    onValueChange: (value: string) => void;
  }) => (
    <select {...props} onChange={(event) => onValueChange(event.target.value)}>
      {children}
    </select>
  ),
  SelectContent: ({ children }: { children: ReactNode }) => <>{children}</>,
  SelectItem: ({ children, value }: { children: ReactNode; value: string }) => (
    <option value={value}>{children}</option>
  ),
  SelectTrigger: () => null,
  SelectValue: () => null,
}));

import {
  RunWorkflowForm,
  getRunWorkflowRequestBody,
  handleRunWorkflowSuccess,
  isOverrideProfilePicked,
  recordRecoveryGuidanceRetryCreated,
  type RunWorkflowFormType,
} from "./RunWorkflowForm";

function formValues(
  overrides: Partial<RunWorkflowFormType>,
): RunWorkflowFormType {
  return {
    webhookCallbackUrl: "",
    proxyLocation: "RESIDENTIAL",
    browserSessionId: null,
    reuseBrowserSession: null,
    browserProfileId: null,
    cdpAddress: null,
    maxScreenshotScrolls: null,
    extraHttpHeaders: null,
    cdpConnectHeaders: null,
    runWith: "agent",
    aiFallback: true,
    ...overrides,
  } as unknown as RunWorkflowFormType;
}

function renderRunWorkflowForm() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/workflows/wpid_test/run"]}>
        <Routes>
          <Route
            path="/workflows/:workflowPermanentId/run"
            element={
              <RunWorkflowForm
                workflowParameters={[]}
                initialValues={{}}
                initialSettings={{
                  proxyLocation: ProxyLocation.Residential,
                  webhookCallbackUrl: "",
                  reuseBrowserSession: false,
                  cdpAddress: null,
                  maxScreenshotScrolls: null,
                  extraHttpHeaders: null,
                  browserProfileId: null,
                  cdpConnectHeaders: null,
                  runWith: "agent",
                }}
              />
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

beforeEach(() => {
  mocks.navigate.mockReset();
  mocks.post.mockReset();
  mocks.post.mockResolvedValue({ data: { workflow_run_id: "wr_test" } });
});

afterEach(() => {
  cleanup();
});

afterAll(() => {
  vi.unstubAllGlobals();
});

describe("getRunWorkflowRequestBody — start-fresh vs profile override (flag-on)", () => {
  test("start fresh drops the (settings-derived) profile override and sets the flag", () => {
    const body = getRunWorkflowRequestBody(
      formValues({ browserProfileId: "bp_x", startFreshBrowser: true }),
      [],
      undefined,
      true,
    );
    expect(body.browser_profile_id).toBeNull();
    expect(body.start_fresh_browser).toBe(true);
  });

  test("a profile override without start fresh is preserved", () => {
    const body = getRunWorkflowRequestBody(
      formValues({ browserProfileId: "bp_x", startFreshBrowser: false }),
      [],
      undefined,
      true,
    );
    expect(body.browser_profile_id).toBe("bp_x");
    expect(body.start_fresh_browser).toBe(false);
  });

  test("no override and no start fresh sends a null profile and a false flag", () => {
    const body = getRunWorkflowRequestBody(formValues({}), [], undefined, true);
    expect(body.browser_profile_id).toBeNull();
    expect(body.start_fresh_browser).toBe(false);
  });

  test("an attached live session suppresses start fresh (backend rejects the combo)", () => {
    const body = getRunWorkflowRequestBody(
      formValues({
        browserSessionId: "pbs_1",
        browserProfileId: "bp_x",
        startFreshBrowser: true,
      }),
      [],
      undefined,
      true,
    );
    expect(body.start_fresh_browser).toBe(false);
    expect(body.browser_session_id).toBe("pbs_1");
    expect(body.browser_profile_id).toBe("bp_x");
  });

  test("a per-input agent drops the rerun-seeded override (backend ranks it above the key)", () => {
    const body = getRunWorkflowRequestBody(
      formValues({ browserProfileId: "bp_x", startFreshBrowser: false }),
      [],
      "user_email",
      true,
    );
    expect(body.browser_profile_id).toBeNull();
  });

  test("a plain agent keeps the rerun-seeded override", () => {
    const body = getRunWorkflowRequestBody(
      formValues({ browserProfileId: "bp_x", startFreshBrowser: false }),
      [],
      null,
      true,
    );
    expect(body.browser_profile_id).toBe("bp_x");
  });

  test("an untouched form sends null to inherit the workflow default", () => {
    const body = getRunWorkflowRequestBody(formValues({}), []);
    expect(body.reuse_browser_session).toBeNull();
  });

  test("an explicit On selection sends true", () => {
    const body = getRunWorkflowRequestBody(
      formValues({ reuseBrowserSession: true }),
      [],
    );
    expect(body.reuse_browser_session).toBe(true);
  });

  test("an explicit Off selection sends false", () => {
    const body = getRunWorkflowRequestBody(
      formValues({ reuseBrowserSession: false }),
      [],
    );
    expect(body.reuse_browser_session).toBe(false);
  });
});

describe("RunWorkflowForm browser session reuse override", () => {
  test("maps Workflow default → On → Off → Workflow default into run payloads", async () => {
    renderRunWorkflowForm();

    const defaultOption = await screen.findByRole("option", {
      name: "Workflow default (currently off)",
    });
    const reuseSelect = defaultOption.parentElement as HTMLSelectElement;
    expect(reuseSelect.tagName).toBe("SELECT");
    expect(reuseSelect.value).toBe("workflow-default");
    const runButton = screen.getByRole("button", { name: "Run agent" });

    const submitAndExpectReuse = async (
      expected: boolean | null,
      expectedCallCount: number,
    ) => {
      fireEvent.click(runButton);
      await waitFor(() =>
        expect(mocks.post).toHaveBeenCalledTimes(expectedCallCount),
      );
      expect(mocks.post.mock.calls[expectedCallCount - 1]?.[1]).toMatchObject({
        reuse_browser_session: expected,
      });
      await waitFor(() =>
        expect(runButton.hasAttribute("disabled")).toBe(false),
      );
    };

    await submitAndExpectReuse(null, 1);

    fireEvent.change(reuseSelect, { target: { value: "on" } });
    expect(reuseSelect.value).toBe("on");
    await submitAndExpectReuse(true, 2);

    fireEvent.change(reuseSelect, { target: { value: "off" } });
    expect(reuseSelect.value).toBe("off");
    await submitAndExpectReuse(false, 3);

    fireEvent.change(reuseSelect, {
      target: { value: "workflow-default" },
    });
    expect(reuseSelect.value).toBe("workflow-default");
    await submitAndExpectReuse(null, 4);
    expect(
      mocks.post.mock.calls.map(([, body]) => body.reuse_browser_session),
    ).toEqual([null, true, false, null]);
  });
});

describe("getRunWorkflowRequestBody — flag-off browser-memory payload", () => {
  test("flag-off omits start_fresh_browser and sends browser-session reuse", () => {
    const body = getRunWorkflowRequestBody(
      formValues({ browserProfileId: "bp_x", startFreshBrowser: false }),
      [],
      "user_email",
      false,
    );
    expect(body).toEqual({
      data: {},
      proxy_location: "RESIDENTIAL",
      browser_session_id: null,
      reuse_browser_session: null,
      browser_profile_id: "bp_x",
      browser_address: null,
      run_with: "agent",
      ai_fallback: true,
    });
  });
});

describe("isOverrideProfilePicked — Start-fresh mutual exclusion", () => {
  test("a per-input rerun does not count as a picked override (Start-fresh stays enabled)", () => {
    expect(isOverrideProfilePicked("bp_x", "user_email", true)).toBe(false);
  });

  test("a plain rerun counts as a picked override (Start-fresh disabled)", () => {
    expect(isOverrideProfilePicked("bp_x", null, true)).toBe(true);
  });

  test("no override is never a picked override", () => {
    expect(isOverrideProfilePicked(null, "user_email", true)).toBe(false);
    expect(isOverrideProfilePicked("", null, true)).toBe(false);
  });
});

describe("recordRecoveryGuidanceRetryCreated", () => {
  test("emits retry_created and produces full navigation only after a returned run id", () => {
    vi.clearAllMocks();
    const context: RecoveryGuidanceTelemetryContext = {
      organizationId: "org_opaque",
      experimentVersion: "sky-13471-recovery-guidance-v1",
      arm: "treatment",
      eligibleRunId: "wr_failure",
      failureCategory: "AUTH_FAILURE",
    };

    expect(recordRecoveryGuidanceRetryCreated(context, undefined)).toBeNull();
    expect(recordRecoveryGuidanceRetryCreated(context, "")).toBeNull();
    expect(recordRecoveryGuidanceRetryCreated(context, "   ")).toBeNull();
    expect(posthog.capture).not.toHaveBeenCalled();

    const recoveryGuidanceRetry = recordRecoveryGuidanceRetryCreated(
      context,
      "wr_retry",
    );
    expect(recoveryGuidanceRetry).toEqual({
      ...context,
      retryRunId: "wr_retry",
    });
    expect(
      getRecoveryGuidanceRetryNavigation({
        recoveryGuidanceRetry,
      }),
    ).toEqual(recoveryGuidanceRetry);
    expect(posthog.capture).toHaveBeenCalledWith(
      "retry_created",
      expect.objectContaining({
        eligible_run_id: "wr_failure",
        retry_run_id: "wr_retry",
        path_id: "retry",
      }),
    );
    expect(posthog.capture).not.toHaveBeenCalledWith(
      "retry_started",
      expect.anything(),
    );
  });
});

describe("handleRunWorkflowSuccess", () => {
  test("preserves success effects for a malformed run id without emitting telemetry", () => {
    vi.clearAllMocks();
    const onStarted = vi.fn();
    const onNavigate = vi.fn();

    handleRunWorkflowSuccess(
      undefined,
      {
        organizationId: "org_opaque",
        experimentVersion: "sky-13471-recovery-guidance-v1",
        arm: "treatment",
        eligibleRunId: "wr_failure",
        failureCategory: "AUTH_FAILURE",
      },
      { onStarted, onNavigate },
    );

    expect(onStarted).toHaveBeenCalledOnce();
    expect(onNavigate).toHaveBeenCalledWith(undefined, null);
    expect(posthog.capture).not.toHaveBeenCalled();
  });
});
