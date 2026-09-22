import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  act,
  createRef,
  isValidElement,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type Ref,
  type ReactNode,
  type SVGProps,
  type TextareaHTMLAttributes,
} from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { ToastAction } from "@/components/ui/toast";
import { toast } from "@/components/ui/use-toast";
import { Link } from "react-router-dom";

import { PromptBox, type PromptBoxHandle } from "./PromptBox";
import {
  PREWARM_DISPATCH_WAIT_TIMEOUT_MS,
  waitForBrowserSessionPrewarm,
} from "./useBrowserSessionPrewarm";

const {
  authState,
  currentOrgState,
  mockNavigate,
  mockPost,
  mockPostHogCapture,
  mockSetAutoplay,
  prewarmFlagState,
} = vi.hoisted(() => ({
  authState: { userId: "user-a" as string | null },
  currentOrgState: { organizationId: "org-a" as string | undefined },
  mockNavigate: vi.fn(),
  mockPost: vi.fn(),
  mockPostHogCapture: vi.fn(),
  mockSetAutoplay: vi.fn(),
  prewarmFlagState: { enabled: false },
}));

vi.mock("@clerk/clerk-react", () => ({
  useAuth: () => ({ userId: authState.userId }),
}));

vi.mock("@/hooks/useCurrentOrgId", () => ({
  useCurrentOrgId: () => currentOrgState.organizationId,
}));

vi.mock("posthog-js", () => ({
  default: { capture: mockPostHogCapture },
}));

const { studioState } = vi.hoisted(() => ({
  studioState: { enabled: false },
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({
    post: mockPost,
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));

vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => studioState.enabled,
}));

vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: () => prewarmFlagState.enabled,
}));

vi.mock("@/store/useAutoplayStore", () => ({
  useAutoplayStore: () => ({
    setAutoplay: mockSetAutoplay,
  }),
}));

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>(
      "react-router-dom",
    );
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

vi.mock("@/components/AutoResizingTextarea/AutoResizingTextarea", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    AutoResizingTextarea: React.forwardRef<
      HTMLTextAreaElement,
      TextareaHTMLAttributes<HTMLTextAreaElement>
    >((props, ref) => <textarea ref={ref} {...props} />),
  };
});

vi.mock("./CyclingPlaceholderTextarea", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    CyclingPlaceholderTextarea: React.forwardRef<
      HTMLTextAreaElement,
      TextareaHTMLAttributes<HTMLTextAreaElement> & { cycling?: boolean }
    >(({ cycling, ...props }, ref) => {
      void cycling;
      return <textarea ref={ref} {...props} />;
    }),
  };
});

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button {...props}>{children}</button>
  ),
}));

vi.mock("@/components/ui/input", () => ({
  Input: (props: InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

vi.mock("@/components/ui/switch", () => ({
  Switch: () => null,
}));

vi.mock("@/components/ui/use-toast", () => ({
  toast: vi.fn(),
}));

vi.mock("@/components/ProxySelector", () => ({
  ProxySelector: () => null,
}));

vi.mock("@/components/KeyValueInput", () => ({
  KeyValueInput: () => null,
}));

vi.mock("@/routes/workflows/components/CodeEditor", () => ({
  CodeEditor: () => null,
}));

vi.mock("@/components/TestWebhookDialog", () => ({
  TestWebhookDialog: ({ trigger }: { trigger: ReactNode }) => <>{trigger}</>,
}));

vi.mock("@/components/ImprovePrompt", () => ({
  ImprovePrompt: () => null,
}));

vi.mock("./ExampleCasePill", () => ({
  ExampleCasePill: ({
    label,
    onClick,
  }: {
    label: string;
    onClick: () => void;
  }) => (
    <button type="button" onClick={onClick}>
      {label}
    </button>
  ),
}));

vi.mock("@radix-ui/react-icons", () => ({
  CalendarIcon: () => null,
  CheckIcon: () => null,
  ClockIcon: () => null,
  CodeIcon: () => null,
  DownloadIcon: () => null,
  EnvelopeClosedIcon: () => null,
  GlobeIcon: () => null,
  LockClosedIcon: () => null,
  TableIcon: () => null,
  TextAlignLeftIcon: () => null,
  ChevronDownIcon: () => null,
  ChevronUpIcon: () => null,
  PlusIcon: () => null,
  UploadIcon: () => null,
  VideoIcon: () => null,
  FileTextIcon: () => null,
  GearIcon: () => null,
  Pencil1Icon: () => null,
  ReloadIcon: () => null,
  PaperPlaneIcon: (props: SVGProps<SVGSVGElement>) => <svg {...props} />,
}));

vi.mock("@/components/icons/CartIcon", () => ({ CartIcon: () => null }));
vi.mock("@/components/icons/GraphIcon", () => ({ GraphIcon: () => null }));
vi.mock("@/components/icons/InboxIcon", () => ({ InboxIcon: () => null }));
vi.mock("@/components/icons/MessageIcon", () => ({ MessageIcon: () => null }));
vi.mock("@/components/icons/TrophyIcon", () => ({ TrophyIcon: () => null }));

function renderPromptBox(
  enableCopilotHandoff = false,
  ref?: Ref<PromptBoxHandle>,
  minimal = false,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <PromptBox
        ref={ref}
        enableCopilotHandoff={enableCopilotHandoff}
        minimal={minimal}
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
});

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  studioState.enabled = true;
  mockNavigate.mockReset();
  mockPost.mockReset();
  mockPostHogCapture.mockReset();
  mockSetAutoplay.mockReset();
  prewarmFlagState.enabled = false;
  authState.userId = "user-a";
  currentOrgState.organizationId = "org-a";
  vi.mocked(toast).mockReset();
  vi.unstubAllGlobals();
});

async function submitPrompt(text: string) {
  renderPromptBox();
  fireEvent.change(screen.getByPlaceholderText("Enter your prompt..."), {
    target: { value: text },
  });
  fireEvent.click(screen.getByLabelText("submit-prompt"));
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
}

describe("PromptBox", () => {
  test.each([
    ["legacy", false, false, "RESIDENTIAL"],
    ["redesigned", false, true, "RESIDENTIAL"],
    ["Copilot handoff", true, true, null],
  ])(
    "prewarms once on first input in the %s Home prompt",
    async (_label, enableCopilotHandoff, minimal, expectedProxyLocation) => {
      prewarmFlagState.enabled = true;
      mockPost.mockResolvedValue({ data: {} });
      renderPromptBox(enableCopilotHandoff, undefined, minimal);

      const textarea = document.getElementById(
        "discover-prompt-input",
      ) as HTMLTextAreaElement;
      expect(textarea).not.toBeNull();
      fireEvent.focus(textarea);
      expect(mockPost).not.toHaveBeenCalled();

      fireEvent.change(textarea, { target: { value: "Start" } });
      await waitFor(() =>
        expect(mockPost).toHaveBeenCalledWith("/debug-session/prewarm", {
          proxy_location: expectedProxyLocation,
        }),
      );

      fireEvent.change(textarea, { target: { value: "Start an agent" } });
      expect(
        mockPost.mock.calls.filter(
          ([path]) => path === "/debug-session/prewarm",
        ),
      ).toHaveLength(1);
    },
  );

  test("caps the editor wait without aborting a stalled prewarm request", async () => {
    vi.useFakeTimers();
    let finishPrewarm: (() => void) | undefined;
    try {
      prewarmFlagState.enabled = true;
      mockPost.mockReturnValue(
        new Promise((resolve) => {
          finishPrewarm = () => resolve({ data: {} });
        }),
      );
      renderPromptBox();

      fireEvent.change(screen.getByPlaceholderText("Enter your prompt..."), {
        target: { value: "Start" },
      });
      await act(async () => undefined);

      let waitFinished = false;
      const wait = waitForBrowserSessionPrewarm().then(() => {
        waitFinished = true;
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(PREWARM_DISPATCH_WAIT_TIMEOUT_MS - 1);
      });
      expect(waitFinished).toBe(false);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      await wait;
      expect(waitFinished).toBe(true);

      finishPrewarm?.();
      await act(async () => undefined);
    } finally {
      vi.useRealTimers();
    }
  });

  test("does not let a stalled request from another identity suppress prewarming", async () => {
    let finishFirstPrewarm: (() => void) | undefined;
    prewarmFlagState.enabled = true;
    mockPost
      .mockReturnValueOnce(
        new Promise((resolve) => {
          finishFirstPrewarm = () => resolve({ data: {} });
        }),
      )
      .mockResolvedValueOnce({ data: {} });

    renderPromptBox();
    fireEvent.change(screen.getByPlaceholderText("Enter your prompt..."), {
      target: { value: "First identity" },
    });
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    cleanup();

    authState.userId = "user-b";
    currentOrgState.organizationId = "org-b";
    renderPromptBox();
    fireEvent.change(screen.getByPlaceholderText("Enter your prompt..."), {
      target: { value: "Second identity" },
    });
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));

    finishFirstPrewarm?.();
    await act(async () => undefined);
  });

  test("releases a stalled client dedupe lock for a later Home mount", async () => {
    vi.useFakeTimers();
    let finishFirstPrewarm: (() => void) | undefined;
    try {
      prewarmFlagState.enabled = true;
      mockPost
        .mockReturnValueOnce(
          new Promise((resolve) => {
            finishFirstPrewarm = () => resolve({ data: {} });
          }),
        )
        .mockResolvedValueOnce({ data: {} });

      renderPromptBox();
      fireEvent.change(screen.getByPlaceholderText("Enter your prompt..."), {
        target: { value: "First mount" },
      });
      await act(async () => undefined);
      expect(mockPost).toHaveBeenCalledTimes(1);
      cleanup();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(PREWARM_DISPATCH_WAIT_TIMEOUT_MS);
      });
      renderPromptBox();
      fireEvent.change(screen.getByPlaceholderText("Enter your prompt..."), {
        target: { value: "Later mount" },
      });
      await act(async () => undefined);
      expect(mockPost).toHaveBeenCalledTimes(2);

      finishFirstPrewarm?.();
      await act(async () => undefined);
    } finally {
      vi.useRealTimers();
    }
  });

  test("prewarms after an imperative prompt prefill without submitting or overwriting typed text", async () => {
    prewarmFlagState.enabled = true;
    mockPost.mockResolvedValue({ data: {} });
    const ref = createRef<PromptBoxHandle>();
    renderPromptBox(false, ref);
    const textarea = screen.getByPlaceholderText("Enter your prompt...");
    textarea.scrollIntoView = vi.fn();

    act(() => ref.current?.focusAndPrefillExample("hackernews"));
    expect((textarea as HTMLTextAreaElement).value).toBe(
      "Navigate to the Hacker News homepage and get the top 3 posts.",
    );
    expect(document.activeElement).toBe(textarea);
    expect(textarea.scrollIntoView).toHaveBeenCalledWith({ block: "center" });
    expect(textarea.getAttribute("id")).toBe("discover-prompt-input");
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/debug-session/prewarm", {
        proxy_location: "RESIDENTIAL",
      }),
    );

    fireEvent.change(textarea, { target: { value: "Keep my agent prompt" } });
    act(() => ref.current?.focusAndPrefillExample("contact_us_forms"));
    expect((textarea as HTMLTextAreaElement).value).toBe(
      "Keep my agent prompt",
    );
    expect(document.activeElement).toBe(textarea);
    expect(
      mockPost.mock.calls.filter(([path]) => path === "/debug-session/prewarm"),
    ).toHaveLength(1);
  });

  test("links a submitted prompt to the created workflow with one attempt id", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_attributed",
        workflow_definition: { blocks: [] },
      },
    });

    await submitPrompt("Visit the docs");

    await waitFor(() =>
      expect(mockPostHogCapture).toHaveBeenCalledWith(
        "home.agent_creation_succeeded",
        expect.objectContaining({
          workflow_permanent_id: "wpid_attributed",
        }),
      ),
    );
    const submitted = mockPostHogCapture.mock.calls.find(
      ([event]) => event === "home.agent_creation_submitted",
    )?.[1];
    const succeeded = mockPostHogCapture.mock.calls.find(
      ([event]) => event === "home.agent_creation_succeeded",
    )?.[1];
    expect(submitted).toMatchObject({
      source: "typed",
      handoff: false,
      variant: "legacy",
    });
    expect(succeeded.attempt_id).toBe(submitted.attempt_id);
    expect(JSON.stringify([submitted, succeeded])).not.toContain(
      "Visit the docs",
    );
  });

  test("assigns a new attempt id when a failed submission is retried", async () => {
    mockPost
      .mockRejectedValueOnce({
        isAxiosError: true,
        response: { status: 422, data: { detail: "Invalid prompt" } },
      })
      .mockResolvedValueOnce({
        data: {
          workflow_permanent_id: "wpid_retry",
          workflow_definition: { blocks: [] },
        },
      });

    renderPromptBox();
    fireEvent.change(screen.getByPlaceholderText("Enter your prompt..."), {
      target: { value: "Visit the docs" },
    });
    fireEvent.click(screen.getByLabelText("submit-prompt"));
    await waitFor(() =>
      expect(mockPostHogCapture).toHaveBeenCalledWith(
        "home.agent_creation_failed",
        expect.objectContaining({ error_category: "invalid_request" }),
      ),
    );

    await waitFor(() =>
      expect(
        (screen.getByLabelText("submit-prompt") as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    fireEvent.click(screen.getByLabelText("submit-prompt"));
    await waitFor(() =>
      expect(mockPostHogCapture).toHaveBeenCalledWith(
        "home.agent_creation_succeeded",
        expect.objectContaining({ workflow_permanent_id: "wpid_retry" }),
      ),
    );

    const submitted = mockPostHogCapture.mock.calls.filter(
      ([event]) => event === "home.agent_creation_submitted",
    );
    const failed = mockPostHogCapture.mock.calls.find(
      ([event]) => event === "home.agent_creation_failed",
    );
    const succeeded = mockPostHogCapture.mock.calls.find(
      ([event]) => event === "home.agent_creation_succeeded",
    );
    expect(submitted).toHaveLength(2);
    expect(submitted[0]?.[1].attempt_id).toBe(failed?.[1].attempt_id);
    expect(submitted[1]?.[1].attempt_id).toBe(succeeded?.[1].attempt_id);
    expect(submitted[1]?.[1].attempt_id).not.toBe(submitted[0]?.[1].attempt_id);
  });

  test("attributes a submitted example without capturing its prompt", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_example",
        workflow_definition: { blocks: [] },
      },
    });

    renderPromptBox();
    fireEvent.click(
      screen.getByRole("button", { name: "Add a product to cart" }),
    );

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const submitted = mockPostHogCapture.mock.calls.find(
      ([event]) => event === "home.agent_creation_submitted",
    )?.[1];
    expect(submitted).toMatchObject({
      source: "example",
      example: "finditparts",
      variant: "legacy",
    });
    expect(JSON.stringify(mockPostHogCapture.mock.calls)).not.toContain(
      "W01-377-8537",
    );
  });

  test("preserves an unedited redesign example through creation outcomes", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_example",
        workflow_definition: { blocks: [] },
      },
    });

    renderPromptBox(false, undefined, true);
    fireEvent.click(screen.getByRole("button", { name: "Apply for a job" }));
    fireEvent.click(screen.getByLabelText("submit-prompt"));

    await waitFor(() =>
      expect(mockPostHogCapture).toHaveBeenCalledWith(
        "home.agent_creation_succeeded",
        expect.objectContaining({ workflow_permanent_id: "wpid_example" }),
      ),
    );

    for (const event of [
      "home.agent_creation_submitted",
      "home.prompt_submitted",
      "home.agent_creation_succeeded",
    ]) {
      expect(mockPostHogCapture).toHaveBeenCalledWith(
        event,
        expect.objectContaining({
          source: "example",
          example: "forms.apply_for_job",
          example_edited: false,
        }),
      );
    }
    expect(JSON.stringify(mockPostHogCapture.mock.calls)).not.toContain(
      "Solutions Engineer",
    );
  });

  test("keeps example origin when the seeded prompt is edited", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_edited_example",
        workflow_definition: { blocks: [] },
      },
    });

    renderPromptBox(false, undefined, true);
    fireEvent.click(screen.getByRole("button", { name: "Scrape a catalog" }));
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, {
      target: {
        value: `${(textarea as HTMLTextAreaElement).value} Include URLs.`,
      },
    });
    fireEvent.click(screen.getByLabelText("submit-prompt"));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPostHogCapture).toHaveBeenCalledWith(
      "home.agent_creation_submitted",
      expect.objectContaining({
        source: "example",
        example: "extract.scrape_catalog",
        example_edited: true,
      }),
    );
  });

  test("clears example attribution after an explicit reset", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_typed_replacement",
        workflow_definition: { blocks: [] },
      },
    });

    renderPromptBox(false, undefined, true);
    fireEvent.click(screen.getByRole("button", { name: "Get a quote" }));
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "" } });
    fireEvent.change(textarea, { target: { value: "Check a public page" } });
    fireEvent.click(screen.getByLabelText("submit-prompt"));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const submitted = mockPostHogCapture.mock.calls.find(
      ([event]) => event === "home.agent_creation_submitted",
    )?.[1];
    expect(submitted).toMatchObject({
      source: "typed",
      variant: "revamp",
    });
    expect(submitted).not.toHaveProperty("example");
    expect(submitted).not.toHaveProperty("example_edited");
  });

  test("replaces attribution when a different example is selected", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_reselected_example",
        workflow_definition: { blocks: [] },
      },
    });

    renderPromptBox(false, undefined, true);
    fireEvent.click(screen.getByRole("button", { name: "Apply for a job" }));
    fireEvent.click(screen.getByRole("button", { name: "Get a quote" }));
    fireEvent.click(screen.getByLabelText("submit-prompt"));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPostHogCapture).toHaveBeenCalledWith(
      "home.agent_creation_submitted",
      expect.objectContaining({
        source: "example",
        example: "forms.get_quote",
        example_edited: false,
      }),
    );
  });

  test("reuses the shipped sample prompts for onboarding intent keys", () => {
    const ref = createRef<PromptBoxHandle>();
    renderPromptBox(false, ref);
    const textarea = screen.getByPlaceholderText(
      "Enter your prompt...",
    ) as HTMLTextAreaElement;
    const cases = [
      ["finditparts", "finditparts.com"],
      ["contact_us_forms", "canadahvac.com/contact-hvac-canada"],
      ["hackernews", "Hacker News homepage"],
      ["AAPLStockPrice", "google finance"],
    ] as const;

    for (const [key, expected] of cases) {
      fireEvent.change(textarea, { target: { value: "" } });
      act(() => ref.current?.focusAndPrefillExample(key));
      expect(textarea.value).toContain(expected);
    }
    expect(mockPost).not.toHaveBeenCalled();
  });

  test("creates prompt-generated workflows as V1 agent runs", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_1",
        workflow_definition: { blocks: [] },
      },
    });

    renderPromptBox();

    expect(screen.queryByText("Skyvern 2.0")).toBeNull();

    fireEvent.change(screen.getByPlaceholderText("Enter your prompt..."), {
      target: { value: "Visit the docs" },
    });
    fireEvent.click(screen.getByLabelText("submit-prompt"));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const call = mockPost.mock.calls[0];
    expect(call).toBeDefined();
    const [path, body] = call!;

    expect(path).toBe("/workflows/create-from-prompt");
    expect(body.task_version).toBe("v1");
    expect(body.request.run_with).toBe("agent");
    expect(body.request.url).toBe("https://google.com");
  });

  // SKY-13154: a 2xx whose body fails JSON.parse arrives as a raw string, so
  // `data.workflow_definition` was undefined and onSuccess threw a TypeError
  // after the success toast had already fired.
  test("rejects a 2xx whose body is not a workflow, without leaking the body", async () => {
    const interstitial = "<html><body>Sign in to continue</body></html>";
    mockPost.mockResolvedValue({
      status: 200,
      headers: { "content-type": "text/html" },
      data: interstitial,
    });

    await submitPrompt("Visit the docs");

    await waitFor(() =>
      expect(vi.mocked(toast)).toHaveBeenCalledWith(
        expect.objectContaining({ variant: "destructive" }),
      ),
    );

    const descriptions = vi
      .mocked(toast)
      .mock.calls.map(([args]) => String(args.description ?? ""));
    const diagnostic = descriptions.join(" ");

    expect(diagnostic).toContain("status=200");
    expect(diagnostic).toContain("content_type=text/html");
    expect(diagnostic).toContain("parsed_as_json=false");
    expect(diagnostic).toContain(`body_length=${interstitial.length}`);
    // The body may be an auth interstitial or carry customer data.
    expect(diagnostic).not.toContain("Sign in to continue");

    expect(
      vi.mocked(toast).mock.calls.some(([args]) => args.variant === "success"),
    ).toBe(false);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  // The agent exists server-side by this point, so an absent first block must
  // not keep the user on /discover.
  test("navigates to the new agent even when it has no blocks", async () => {
    mockPost.mockResolvedValue({
      status: 200,
      headers: { "content-type": "application/json" },
      data: {
        workflow_permanent_id: "wpid_empty",
        workflow_definition: { blocks: [] },
      },
    });

    await submitPrompt("Visit the docs");

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledTimes(1));
    expect(mockNavigate).toHaveBeenCalledWith("/agents/wpid_empty/studio");
    expect(mockSetAutoplay).not.toHaveBeenCalled();
  });

  test("hands Discover prompts to workflow studio with recoverable prompt state", async () => {
    studioState.enabled = true;
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_studio",
        workflow_definition: { blocks: [] },
      },
    });

    renderPromptBox(true);

    fireEvent.change(screen.getByPlaceholderText("Enter your prompt..."), {
      target: { value: "Build this in studio" },
    });
    fireEvent.click(screen.getByLabelText("submit-prompt"));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));

    expect(mockNavigate).toHaveBeenCalledWith(
      "/agents/wpid_studio/studio?via=discover",
      {
        state: { copilotMessage: "Build this in studio" },
      },
    );
    expect(
      sessionStorage.getItem("skyvern.discoverCopilotHandoff:wpid_studio"),
    ).toBe("Build this in studio");
  });

  // SKY-15589: the axios message ("Request failed with status code 402") hid
  // the server's billing guidance behind a generic toast.
  test.each([
    { handoff: false, status: 402 },
    { handoff: true, status: 402 },
    { handoff: false, status: 422 },
    { handoff: true, status: 422 },
  ])(
    "surfaces the server detail on a $status (handoff=$handoff)",
    async ({ handoff, status }) => {
      const detail =
        status === 402
          ? "Credits exhausted. Upgrade your plan in Billing."
          : "Prompt must be at least 10 characters.";
      mockPost.mockRejectedValue({
        isAxiosError: true,
        message: `Request failed with status code ${status}`,
        response: { status, data: { detail } },
      });

      renderPromptBox(handoff);
      fireEvent.change(screen.getByPlaceholderText("Enter your prompt..."), {
        target: { value: "Visit the docs" },
      });
      fireEvent.click(screen.getByLabelText("submit-prompt"));

      await waitFor(() =>
        expect(vi.mocked(toast)).toHaveBeenCalledWith(
          expect.objectContaining({
            variant: "destructive",
            description: detail,
          }),
        ),
      );
      const args = vi.mocked(toast).mock.calls[0]![0];
      if (status === 402) {
        expect(args.title).toBe("Not enough credits");
        const action = args.action;
        if (
          !isValidElement<{
            altText: string;
            asChild?: boolean;
            children: unknown;
          }>(action)
        ) {
          throw new Error("402 toast has no action element");
        }
        expect(action.type).toBe(ToastAction);
        expect(action.props.altText).toBe("Go to Billing");
        expect(action.props.asChild).toBe(true);
        const link = action.props.children;
        if (!isValidElement<{ to: string; children: unknown }>(link)) {
          throw new Error("402 toast action has no link child");
        }
        expect(link.type).toBe(Link);
        expect(link.props.to).toBe("/billing");
        expect(link.props.children).toBe("Go to Billing");
      } else {
        expect(args.title).toBe(
          handoff ? "Error creating agent" : "Error creating agent from prompt",
        );
        expect(args.action).toBeUndefined();
      }
      expect(mockNavigate).not.toHaveBeenCalled();
    },
  );
});
