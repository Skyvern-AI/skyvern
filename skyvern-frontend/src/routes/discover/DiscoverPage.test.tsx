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
  QuestionnairePatchV1,
  QuestionnaireStateV1,
} from "@/store/onboarding/types";
import { OnboardingContext } from "@/store/onboarding/useOnboardingState";
import { DiscoverPage } from "./DiscoverPage";

const mocks = vi.hoisted(() => ({
  confirmed: vi.fn<(patch: ConfirmedPatch) => Promise<ConfirmedWriteResult>>(),
  createPending: false,
  createWorkflow: vi.fn(),
  focusAndPrefillExample: vi.fn<(key: string) => void>(),
  telemetry: {
    registerVariant: vi.fn(),
    flowStarted: vi.fn(),
    modalOpened: vi.fn(),
    questionnaireShown: vi.fn<(input: unknown) => boolean>(() => true),
    questionnaireCompleted: vi.fn(),
    modalRenderError: vi.fn(),
  },
}));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagVariantKey: () => "template-first",
}));
vi.mock("@clerk/clerk-react", () => ({
  useAuth: () => ({ userId: "user-a" }),
  useUser: () => ({
    isLoaded: true,
    user: { createdAt: new Date("2026-08-28T00:00:00Z") },
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
  useCreateWorkflowMutation: () => ({
    mutate: mocks.createWorkflow,
    isPending: mocks.createPending,
  }),
}));
vi.mock("@/routes/tasks/create/PromptBox", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    PromptBox: React.forwardRef<
      { focusAndPrefillExample: (key: string) => void },
      Record<string, never>
    >(function PromptBoxMock(_, ref) {
      const [value, setValue] = React.useState("");
      const textareaRef = React.useRef<HTMLTextAreaElement>(null);
      React.useImperativeHandle(ref, () => ({
        focusAndPrefillExample: (key) => {
          mocks.focusAndPrefillExample(key);
          setValue((current) => (current.trim() ? current : key));
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
vi.mock("@/components/onboarding/QuestionnaireDetailsStep", () => ({
  QuestionnaireDetailsStep: ({
    expectedRevision,
    onAction,
  }: {
    expectedRevision: number;
    onAction: (patch: QuestionnairePatchV1) => Promise<void>;
  }) => (
    <button
      type="button"
      onClick={() =>
        void onAction({
          version: 1,
          mutation_id: `complete-${expectedRevision}`,
          expected_revision: expectedRevision,
          action: "complete",
          role: "developer",
          company_context: "startup",
          scale_intent: "exploring",
          referral_source: "search",
        })
      }
    >
      details-submit
    </button>
  ),
}));
vi.mock("@/util/onboarding/OnboardingTelemetry", () => ({
  OnboardingTelemetry: mocks.telemetry,
}));
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

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
      "contact_us_forms",
    );
    expect((prompt as HTMLTextAreaElement).value).toBe("contact_us_forms");
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
      "contact_us_forms",
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
  it("preserves content order and mounts over seeded template data", async () => {
    renderDiscover(baseState);
    const content = screen.getByTestId("discover-templates").parentElement;
    expect(content?.textContent).toBe(
      "Create an agentpromptSkip — start with blank canvas →templates",
    );
    expect(screen.queryByText("Build your first agent")).toBeNull();
    expect(screen.queryByText(/Keep going/)).toBeNull();
    expect(screen.queryByText("Resume getting started")).toBeNull();
    expect(await screen.findByRole("dialog")).toBeTruthy();
  });

  it("keeps Discover actions behind the modal while reservation is pending", async () => {
    const reservation = deferred<ConfirmedWriteResult>();
    mocks.confirmed.mockReturnValueOnce(reservation.promise);
    const view = renderDiscover(baseState);
    await waitFor(() => expect(mocks.confirmed).toHaveBeenCalledOnce());
    const dialog = screen.getByRole("dialog");
    expect(
      screen.getByRole("heading", { name: "Getting started" }),
    ).toBeTruthy();
    expect(screen.getByText("Checking your onboarding setup.")).toBeTruthy();
    expect(mocks.telemetry.modalOpened).not.toHaveBeenCalled();
    expect(mocks.telemetry.questionnaireShown).not.toHaveBeenCalled();
    const blankCanvas = screen.getByRole("button", {
      name: /start with blank canvas/i,
      hidden: true,
    });

    blankCanvas.focus();
    await waitFor(() =>
      expect(dialog.contains(document.activeElement)).toBe(true),
    );
    fireEvent.keyDown(document.activeElement!, { key: "Enter" });
    expect(mocks.createWorkflow).not.toHaveBeenCalled();

    view.unmount();
    await act(async () =>
      reservation.resolve({
        onboarding_state: {
          ...baseState,
          questionnaire_prompted_at: "2026-08-27T00:00:00Z",
        },
        launch_date_at_signup: "2026-01-01T00:00:00Z",
        recovery_guidance_assignment: null,
        questionnaire_prompt_result: {
          status: "reserved",
          prompted_at: "2026-08-27T00:00:00Z",
        },
      }),
    );
    expect(mocks.telemetry.questionnaireShown).not.toHaveBeenCalled();
  });

  it("shields an eligible older-org user with a prior save", async () => {
    const reservation = deferred<ConfirmedWriteResult>();
    const firstSaveAt = "2026-08-20T00:00:00Z";
    mocks.confirmed.mockReturnValueOnce(reservation.promise);
    renderDiscover({ ...baseState, first_save_at: firstSaveAt }, false, false);
    await waitFor(() => expect(mocks.confirmed).toHaveBeenCalledOnce());
    const dialog = screen.getByRole("dialog");
    const blankCanvas = screen.getByRole("button", {
      name: /start with blank canvas/i,
      hidden: true,
    });
    expect(
      screen.getByRole("heading", { name: "Getting started" }),
    ).toBeTruthy();

    blankCanvas.focus();
    await waitFor(() =>
      expect(dialog.contains(document.activeElement)).toBe(true),
    );
    expect(mocks.createWorkflow).not.toHaveBeenCalled();

    await act(async () =>
      reservation.resolve({
        onboarding_state: {
          ...baseState,
          first_save_at: firstSaveAt,
          questionnaire_prompted_at: "2026-08-27T00:00:00Z",
        },
        launch_date_at_signup: "2026-01-01T00:00:00Z",
        recovery_guidance_assignment: null,
        questionnaire_prompt_result: {
          status: "reserved",
          prompted_at: "2026-08-27T00:00:00Z",
        },
      }),
    );
    expect(
      await screen.findByRole("heading", {
        name: "What do you want to automate?",
      }),
    ).toBeTruthy();
    expect(mocks.telemetry.questionnaireShown).toHaveBeenCalledOnce();
  });

  it("renders without onboarding UI when no provider exists", () => {
    render(
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mocks.telemetry.modalRenderError).not.toHaveBeenCalled();
  });

  it("routes stored intent into the editor fallback", async () => {
    renderDiscover({ ...baseState, user_intent: "fill_forms" });
    expect(await screen.findByText("Pick a template to start")).toBeTruthy();
  });

  it("keeps an established user without a pending questionnaire closed", async () => {
    renderDiscover(
      { ...baseState, user_intent: "fill_forms", questionnaire: null },
      false,
      false,
    );
    await waitFor(() => expect(mocks.confirmed).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryByText("Getting started")).toBeNull();
  });

  it("keeps a completed questionnaire closed", () => {
    renderDiscover({
      ...baseState,
      user_intent: "fill_forms",
      questionnaire_prompted_at: "2026-08-27T00:00:00Z",
      questionnaire: questionnaire(true),
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mocks.confirmed).not.toHaveBeenCalled();
  });

  it("never reopens a legacy deferred response", () => {
    const deferredState = {
      ...baseState,
      user_intent: "fill_forms",
      questionnaire: questionnaire(),
    };
    mocks.confirmed.mockResolvedValue({
      onboarding_state: {
        ...deferredState,
        questionnaire: questionnaire(true),
      },
      launch_date_at_signup: "2026-01-01T00:00:00Z",
      recovery_guidance_assignment: null,
    });

    renderDiscover(deferredState, true, false);
    expect(screen.queryByText("Pick a template to start")).toBeNull();
    expect(mocks.telemetry.questionnaireShown).not.toHaveBeenCalled();
    expect(mocks.confirmed).not.toHaveBeenCalled();
  });
});
