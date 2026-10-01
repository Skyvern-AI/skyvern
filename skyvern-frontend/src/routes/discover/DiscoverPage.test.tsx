// @vitest-environment jsdom
import { StrictMode, useEffect, useRef, useState, type ReactNode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  MemoryRouter,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ConfirmedPatch,
  ConfirmedWriteResult,
  OnboardingState,
  QuestionnaireStateV1,
} from "@/store/onboarding/types";
import { OnboardingContext } from "@/store/onboarding/useOnboardingState";
import { DiscoverPage } from "./DiscoverPage";

const mocks = vi.hoisted(() => ({
  capture: vi.fn(),
  confirmed: vi.fn<(patch: ConfirmedPatch) => Promise<ConfirmedWriteResult>>(),
  createPending: false,
  createWorkflow: vi.fn(),
  createWorkflowOptions: undefined as { onCreated?: () => void } | undefined,
  focusAndPrefillExample:
    vi.fn<(key: string | null, fallback: string) => void>(),
  homeViewed: vi.fn(),
  telemetry: {
    registerVariant: vi.fn(),
    questionnaireShown: vi.fn<(input: unknown) => boolean>(() => true),
    questionnaireCompleted: vi.fn(),
  },
}));

vi.mock("posthog-js", () => ({
  default: { capture: mocks.capture },
}));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagVariantKey: () => "template-first",
}));
vi.mock("@/hooks/useUser", () => ({
  useUser: () => ({
    get: () => ({
      id: "user-a",
      email: "",
      name: "",
      createdAt: new Date("2026-08-28T00:00:00Z"),
    }),
  }),
}));
vi.mock("@/routes/workflows/hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({
    data: [
      {
        workflow_permanent_id: "seeded-template",
        title: "Seeded template",
        description: "Template fixture",
        workflow_definition: { blocks: [] },
      },
    ],
    isLoading: false,
  }),
}));
vi.mock("@/routes/workflows/hooks/useCreateWorkflowMutation", () => ({
  useCreateWorkflowMutation: (options?: { onCreated?: () => void }) => {
    mocks.createWorkflowOptions = options;
    return {
      mutate: mocks.createWorkflow,
      isPending: mocks.createPending,
    };
  },
}));
vi.mock("@/routes/tasks/create/PromptBox", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    PromptBox: React.forwardRef<
      {
        focusAndPrefillExample: (key: string | null, fallback: string) => void;
      },
      { secondaryAction?: React.ReactNode }
    >(function PromptBoxMock({ secondaryAction }, ref) {
      const [value, setValue] = React.useState("");
      const textareaRef = React.useRef<HTMLTextAreaElement>(null);
      React.useImperativeHandle(ref, () => ({
        focusAndPrefillExample: (key, fallback) => {
          mocks.focusAndPrefillExample(key, fallback);
          setValue((current) => (current.trim() ? current : (key ?? fallback)));
          textareaRef.current?.focus({ preventScroll: true });
        },
      }));
      return (
        <div data-testid="discover-prompt">
          prompt
          <textarea
            ref={textareaRef}
            aria-label="Discover prompt"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
          {secondaryAction}
        </div>
      );
    }),
  };
});
vi.mock("./WorkflowTemplates", () => ({
  WorkflowTemplates: () => (
    <div data-testid="discover-templates">templates</div>
  ),
}));
vi.mock("@/util/onboarding/OnboardingTelemetry", () => ({
  OnboardingTelemetry: mocks.telemetry,
}));
vi.mock("@/util/homeTelemetry", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/util/homeTelemetry")>();
  return {
    ...actual,
    HomeTelemetry: {
      ...actual.HomeTelemetry,
      viewed: mocks.homeViewed,
    },
  };
});
const baseState: OnboardingState = {
  tour_completed_at: null,
  modal_dismissed_at: null,
  first_save_at: null,
  first_run_at: null,
  ab_variant: "template-first",
  user_intent: null,
  questionnaire: null,
  questionnaire_prompted_at: null,
  seen_canvas: null,
  seen_node_adder: null,
  seen_sidebar: null,
  seen_save_run: null,
};
const questionnaire = (completed = false): QuestionnaireStateV1 => ({
  version: 1,
  response_id: "response",
  revision: 1,
  last_mutation_id: completed ? "complete-1" : "defer-1",
  status: completed ? "completed" : "deferred",
  role: completed ? "developer" : null,
  company_context: completed ? "startup" : null,
  scale_intent: completed ? "exploring" : null,
  referral_source: completed ? "search" : null,
  completed_at: completed ? "2026-01-01T00:00:00Z" : null,
  skipped_at: null,
  deferred_at: completed ? null : "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  defer_prompt_count: completed ? 0 : 1,
});

function LocationProbe() {
  return <span data-testid="location">{useLocation().search}</span>;
}

function NavigationProbe() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate("/discover?focus=prompt")}>
      Focus prompt
    </button>
  );
}

function PlanCleanup({ children }: { children: ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const handled = useRef(false);
  useEffect(() => {
    if (handled.current || !searchParams.has("plan")) return;
    handled.current = true;
    const next = new URLSearchParams(searchParams);
    next.delete("plan");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);
  return <>{children}</>;
}

function TestProvider({
  initialState,
  isNewUser = true,
  initialEntries,
  isLoading = false,
  stateOverride,
  showNavigation = false,
  withPlanCleanup = false,
}: {
  initialState: OnboardingState;
  isNewUser?: boolean;
  initialEntries?: string[];
  isLoading?: boolean;
  stateOverride?: OnboardingState | null;
  showNavigation?: boolean;
  withPlanCleanup?: boolean;
}) {
  const [state, setState] = useState(initialState);
  return (
    <OnboardingContext.Provider
      value={{
        state: stateOverride === undefined ? state : stateOverride,
        isLoading,
        updateState: (patch) =>
          setState((current) => ({ ...current, ...patch })),
        updateStateConfirmed: async (patch) => {
          const next = await mocks.confirmed(patch);
          if ("onboarding_state" in next) {
            setState(next.onboarding_state);
          }
          return next;
        },
        isNewUser,
        abVariant: "template-first",
        recoveryGuidanceAssignment: null,
      }}
    >
      <MemoryRouter initialEntries={initialEntries}>
        {withPlanCleanup ? (
          <PlanCleanup>
            <DiscoverPage />
          </PlanCleanup>
        ) : (
          <DiscoverPage />
        )}
        <LocationProbe />
        {showNavigation ? <NavigationProbe /> : null}
      </MemoryRouter>
    </OnboardingContext.Provider>
  );
}

function renderDiscover(
  initialState: OnboardingState,
  strict = false,
  isNewUser = true,
  initialEntries?: string[],
) {
  const page = (
    <TestProvider
      initialState={initialState}
      isNewUser={isNewUser}
      initialEntries={initialEntries}
    />
  );
  return render(strict ? <StrictMode>{page}</StrictMode> : page);
}

beforeEach(() => {
  sessionStorage.clear();
  mocks.createPending = false;
  mocks.createWorkflowOptions = undefined;
  mocks.confirmed.mockResolvedValue({
    onboarding_state: baseState,
    launch_date_at_signup: "2026-01-01T00:00:00Z",
    recovery_guidance_assignment: null,
    questionnaire_prompt_result: { status: "flag_disabled" },
  });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DiscoverPage focus param", () => {
  const resolvedState = {
    ...baseState,
    user_intent: "fill_forms",
    questionnaire_prompted_at: "2026-08-27T00:00:00Z",
    questionnaire: questionnaire(true),
  };

  it("focuses and prefills once, preserving unrelated search params", async () => {
    renderDiscover(resolvedState, false, true, [
      "/discover?focus=prompt&foo=bar",
    ]);
    const prompt = await screen.findByLabelText("Discover prompt");
    await waitFor(() =>
      expect(mocks.focusAndPrefillExample).toHaveBeenCalledOnce(),
    );
    expect(mocks.focusAndPrefillExample).toHaveBeenCalledWith(
      null,
      "add_employee",
    );
    expect((prompt as HTMLTextAreaElement).value).toBe("add_employee");
    expect(document.activeElement).toBe(prompt);
    expect(screen.getByTestId("location").textContent).toBe("?foo=bar");
    expect(mocks.createWorkflow).not.toHaveBeenCalled();
  });

  it("waits for onboarding intent before consuming the focus param", async () => {
    const view = render(
      <TestProvider
        initialState={resolvedState}
        stateOverride={null}
        isLoading
        initialEntries={["/discover?focus=prompt"]}
      />,
    );
    expect(mocks.focusAndPrefillExample).not.toHaveBeenCalled();
    expect(screen.getByTestId("location").textContent).toBe("?focus=prompt");

    view.rerender(
      <TestProvider
        initialState={resolvedState}
        stateOverride={resolvedState}
        initialEntries={["/discover?focus=prompt"]}
      />,
    );

    await waitFor(() =>
      expect(mocks.focusAndPrefillExample).toHaveBeenCalledOnce(),
    );
    expect(mocks.focusAndPrefillExample).toHaveBeenCalledWith(
      null,
      "add_employee",
    );
    expect(screen.getByTestId("location").textContent).toBe("");
  });

  it("prefills exactly once in StrictMode", async () => {
    renderDiscover(resolvedState, true, true, ["/discover?focus=prompt"]);
    await waitFor(() =>
      expect(mocks.focusAndPrefillExample).toHaveBeenCalledOnce(),
    );
  });

  it("does nothing when the focus param is absent", () => {
    renderDiscover(resolvedState, false, true, ["/discover?foo=bar"]);
    expect(mocks.focusAndPrefillExample).not.toHaveBeenCalled();
    expect(screen.getByTestId("location").textContent).toBe("?foo=bar");
  });

  it("preserves a typed draft during same-page focus navigation", async () => {
    render(
      <TestProvider
        initialState={resolvedState}
        initialEntries={["/discover"]}
        showNavigation
      />,
    );
    const prompt = screen.getByLabelText("Discover prompt");
    fireEvent.change(prompt, { target: { value: "my draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Focus prompt" }));

    await waitFor(() =>
      expect(mocks.focusAndPrefillExample).toHaveBeenCalledOnce(),
    );
    expect((prompt as HTMLTextAreaElement).value).toBe("my draft");
    expect(document.activeElement).toBe(prompt);
    expect(screen.getByTestId("location").textContent).toBe("");
  });

  it("handles a later same-page focus navigation again", async () => {
    render(
      <TestProvider
        initialState={resolvedState}
        initialEntries={["/discover?focus=prompt&foo=bar"]}
        showNavigation
      />,
    );
    await waitFor(() =>
      expect(mocks.focusAndPrefillExample).toHaveBeenCalledOnce(),
    );
    expect(screen.getByTestId("location").textContent).toBe("?foo=bar");
    fireEvent.click(screen.getByRole("button", { name: "Focus prompt" }));
    await waitFor(() =>
      expect(mocks.focusAndPrefillExample).toHaveBeenCalledTimes(2),
    );
    expect(screen.getByTestId("location").textContent).toBe("");
  });

  it("strips the param again when an ancestor writer restores it in the same commit", async () => {
    render(
      <TestProvider
        initialState={resolvedState}
        initialEntries={["/discover?focus=prompt&plan=pro&foo=bar"]}
        withPlanCleanup
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("?foo=bar"),
    );
    expect(mocks.focusAndPrefillExample).toHaveBeenCalledOnce();
  });
});

describe("DiscoverPage onboarding mount", () => {
  it("records the redesign variant as the canonical exposure", () => {
    render(
      <MemoryRouter>
        <DiscoverPage revamp />
      </MemoryRouter>,
    );

    expect(mocks.homeViewed).toHaveBeenCalledOnce();
    expect(mocks.homeViewed).toHaveBeenCalledWith("revamp");
  });

  it("records one canonical exposure when the rendered variant changes", () => {
    const view = render(
      <MemoryRouter>
        <DiscoverPage revamp={false} />
      </MemoryRouter>,
    );

    expect(mocks.homeViewed).toHaveBeenCalledOnce();
    expect(mocks.homeViewed).toHaveBeenCalledWith("legacy");

    view.rerender(
      <MemoryRouter>
        <DiscoverPage revamp />
      </MemoryRouter>,
    );

    expect(mocks.homeViewed).toHaveBeenCalledOnce();
  });

  it("preserves content order and mounts over seeded template data", () => {
    renderDiscover(baseState);
    const content =
      screen.getByTestId("discover-templates").parentElement?.parentElement;
    expect(content?.textContent).toBe(
      "Create an agentpromptStart with a blank canvastemplates",
    );
    expect(screen.queryByText("Build your first agent")).toBeNull();
    expect(screen.queryByText(/Keep going/)).toBeNull();
    expect(screen.queryByText("Resume getting started")).toBeNull();
  });

  it("starts one attributed blank-agent attempt", () => {
    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );

    fireEvent.click(
      screen.getByRole("button", { name: /start with a blank canvas/i }),
    );

    const submitted = mocks.capture.mock.calls.find(
      ([event]) => event === "home.agent_creation_submitted",
    )?.[1];
    expect(submitted).toMatchObject({
      source: "blank",
      handoff: false,
      variant: "legacy",
    });
    expect(mocks.createWorkflow).toHaveBeenCalledWith(
      expect.objectContaining({
        _via: "blank",
        _agentCreationAttempt: expect.objectContaining({
          attemptId: submitted.attempt_id,
          source: "blank",
          variant: "legacy",
        }),
      }),
      expect.any(Object),
    );
  });

  it("never opens a dialog or reserves the questionnaire for an eligible new user", async () => {
    renderDiscover(baseState);
    await act(async () => {});
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mocks.confirmed).not.toHaveBeenCalled();
  });
});

describe("DiscoverPage redesigned blank agent", () => {
  it("creates one blank agent through the existing path", () => {
    const onRevampComplete = vi.fn();
    render(
      <MemoryRouter>
        <DiscoverPage revamp onRevampComplete={onRevampComplete} />
      </MemoryRouter>,
    );

    const action = screen.getByRole("button", {
      name: "Skip — start from a blank agent",
    });
    fireEvent.click(action);
    expect(
      mocks.capture.mock.calls.filter(
        ([event]) => event === "home.skip_blank_canvas_clicked",
      ),
    ).toHaveLength(1);
    fireEvent.click(action);

    expect(
      mocks.capture.mock.calls.filter(
        ([event]) => event === "home.skip_blank_canvas_clicked",
      ),
    ).toHaveLength(1);
    expect(mocks.createWorkflow).toHaveBeenCalledOnce();
    expect(mocks.createWorkflow).toHaveBeenCalledWith(
      expect.objectContaining({
        _via: "blank",
        title: "New Agent",
        workflow_definition: expect.objectContaining({
          blocks: [],
          parameters: [],
        }),
      }),
      expect.objectContaining({ onSettled: expect.any(Function) }),
    );
    expect(mocks.createWorkflowOptions?.onCreated).toBe(onRevampComplete);
  });

  it("disables the blank action while creation is pending", () => {
    mocks.createPending = true;
    render(
      <MemoryRouter>
        <DiscoverPage revamp />
      </MemoryRouter>,
    );

    const action = screen.getByRole("button", {
      name: "Skip — start from a blank agent",
    });
    expect(action.hasAttribute("disabled")).toBe(true);
    fireEvent.click(action);
    expect(mocks.createWorkflow).not.toHaveBeenCalled();
  });
});
