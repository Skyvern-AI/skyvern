// @vitest-environment jsdom
import { StrictMode, useState } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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
import { onboardingExampleRequest } from "./onboardingExample";

type ProgressState = "active" | "dismissed" | "completed" | "ineligible";
type ProgressActionKey = "first_agent_created" | "first_successful_run";

const mocks = vi.hoisted(() => ({
  confirmed: vi.fn<(patch: ConfirmedPatch) => Promise<ConfirmedWriteResult>>(),
  progressState: null as ProgressState | null,
  completedCount: 0 as 0 | 1,
  firstMilestoneComplete: false,
  nextActionKey: "first_agent_created" as ProgressActionKey,
  createPending: false,
  createWorkflow: vi.fn(),
  progressPending: false,
  dismiss: vi.fn(),
  restore: vi.fn(),
  telemetry: {
    registerVariant: vi.fn(),
    flowStarted: vi.fn(),
    modalOpened: vi.fn(),
    questionnaireShown: vi.fn<(input: unknown) => boolean>(() => true),
    questionnaireCompleted: vi.fn(),
    modalRenderError: vi.fn(),
  },
}));

function progressForMocks() {
  if (mocks.progressState === null) return null;
  if (mocks.progressState !== "active") {
    return { state: mocks.progressState };
  }
  return {
    state: mocks.progressState,
    completed_count: mocks.completedCount,
    total_count: 2,
    next_action_key: mocks.nextActionKey,
    items: [
      {
        key: "first_agent_created",
        completed_at: mocks.firstMilestoneComplete
          ? "2026-08-20T12:00:00Z"
          : null,
      },
      { key: "first_successful_run", completed_at: null },
    ],
  };
}

vi.mock("posthog-js/react", () => ({
  useFeatureFlagVariantKey: () => "template-first",
}));
vi.mock("@clerk/clerk-react", () => ({
  useAuth: () => ({ userId: "user-a" }),
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
vi.mock("@/routes/tasks/create/PromptBox", () => ({
  PromptBox: () => <div data-testid="discover-prompt">prompt</div>,
}));
vi.mock("./WorkflowTemplates", () => ({
  WorkflowTemplates: () => (
    <div data-testid="discover-templates">templates</div>
  ),
}));
vi.mock("./useOnboardingProgress", () => ({
  useOnboardingProgress: () => ({
    progress: progressForMocks(),
    isPending: mocks.progressPending,
    dismiss: mocks.dismiss,
    restore: mocks.restore,
  }),
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

function TestProvider({
  initialState,
  isNewUser = true,
}: {
  initialState: OnboardingState;
  isNewUser?: boolean;
}) {
  const [state, setState] = useState(initialState);
  return (
    <OnboardingContext.Provider
      value={{
        state,
        isLoading: false,
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
      <MemoryRouter>
        <DiscoverPage />
      </MemoryRouter>
    </OnboardingContext.Provider>
  );
}

function renderDiscover(
  initialState: OnboardingState,
  strict = false,
  isNewUser = true,
) {
  const page = (
    <TestProvider initialState={initialState} isNewUser={isNewUser} />
  );
  return render(strict ? <StrictMode>{page}</StrictMode> : page);
}

beforeEach(() => {
  sessionStorage.clear();
  mocks.progressState = null;
  mocks.createPending = false;
  mocks.completedCount = 0;
  mocks.firstMilestoneComplete = false;
  mocks.nextActionKey = "first_agent_created";
  mocks.progressPending = false;
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

describe("DiscoverPage onboarding mount", () => {
  it("preserves content order and mounts over seeded template data", async () => {
    renderDiscover(baseState);
    const content = screen.getByTestId("discover-templates").parentElement;
    expect(content?.textContent).toBe(
      "Create an agentpromptSkip — start with blank canvas →templates",
    );
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
    render(<DiscoverPage />);
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

describe("DiscoverPage progress surfaces", () => {
  it("renders after PromptBox without an eager write, single-flights creation, and preserves focus on dismissal", () => {
    mocks.progressState = "active";
    const { container, rerender } = render(<DiscoverPage />);
    expect(container.innerHTML).toMatch(
      /<h1[^>]*>Create an agent<\/h1>.*prompt.*See how a workflow run is organized.*Build your first agent.*templates/s,
    );
    expect(mocks.createWorkflow).not.toHaveBeenCalled();

    const workingExampleLink = screen.getByRole("link", {
      name: "Use the working example",
    });
    const workingExampleHeading = screen.getByRole("heading", {
      name: "See how a workflow run is organized",
    });
    expect(workingExampleLink.getAttribute("href")).toBe(
      "#working-example-heading",
    );
    expect(workingExampleHeading.getAttribute("tabindex")).toBe("-1");
    workingExampleLink.focus();
    expect(document.activeElement).toBe(workingExampleLink);
    fireEvent.keyDown(workingExampleLink, { key: "Enter" });
    fireEvent.click(workingExampleLink);
    expect(document.activeElement).toBe(workingExampleHeading);
    const copy = screen.getByRole("button", { name: "Make a copy" });
    const skip = screen.getByRole("button", {
      name: /Skip — start with blank canvas/,
    });
    fireEvent.click(copy);
    fireEvent.click(skip);
    expect(mocks.createWorkflow).toHaveBeenCalledTimes(1);
    expect(mocks.createWorkflow).toHaveBeenCalledWith(
      {
        ...onboardingExampleRequest,
        _via: "onboarding_example",
      },
      { onSettled: expect.any(Function) },
    );

    mocks.createWorkflow.mock.calls[0]?.[1]?.onSettled?.();
    fireEvent.click(skip);
    expect(mocks.createWorkflow).toHaveBeenCalledTimes(2);

    const hide = screen.getByRole("button", { name: "Hide setup" });
    hide.focus();
    fireEvent.click(hide);
    expect(mocks.dismiss).toHaveBeenCalledTimes(1);

    mocks.progressPending = true;
    rerender(<DiscoverPage />);
    const pendingHide = screen.getByRole("button", { name: "Hide setup" });
    expect(pendingHide).toBe(hide);
    expect(document.activeElement).toBe(pendingHide);
    expect(pendingHide.getAttribute("aria-disabled")).toBe("true");
    expect(pendingHide.hasAttribute("disabled")).toBe(false);
    fireEvent.click(pendingHide);
    expect(mocks.dismiss).toHaveBeenCalledTimes(1);

    mocks.progressState = "dismissed";
    mocks.progressPending = false;
    rerender(<DiscoverPage />);
    const resume = screen.getByRole("button", { name: "Resume setup" });
    expect(resume).toBe(hide);
    expect(document.activeElement).toBe(resume);
    expect(
      screen.queryByText("See how a workflow run is organized"),
    ).toBeNull();
    fireEvent.click(resume);
    expect(mocks.restore).toHaveBeenCalledTimes(1);
  });

  it.each(["completed", "ineligible", null] as const)(
    "retires all progress surfaces for %s progress",
    (state) => {
      mocks.progressState = state;
      render(<DiscoverPage />);
      expect(document.body.textContent).not.toMatch(
        /Build your first agent|See how a workflow run is organized|Hide setup|Resume setup/,
      );
    },
  );
});
