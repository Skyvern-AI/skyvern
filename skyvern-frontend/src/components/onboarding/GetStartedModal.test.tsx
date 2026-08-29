// @vitest-environment jsdom
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
  ConfirmedWriteResult,
  OnboardingState,
  QuestionnaireStateV1,
} from "@/store/onboarding/types";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";

const {
  mockUpdateState,
  mockUpdateStateConfirmed,
  mockMutate,
  mockTelemetry,
  mutationState,
  flagState,
  stateOverrides,
  clerkUser,
} = vi.hoisted(() => ({
  mockUpdateState: vi.fn(),
  mockUpdateStateConfirmed: vi.fn(),
  mockMutate: vi.fn(),
  mockTelemetry: {
    registerVariant: vi.fn(),
    flowStarted: vi.fn(),
    modalOpened: vi.fn(),
    abVariantAssigned: vi.fn(),
    modalSkipped: vi.fn(),
    modalTemplateSelected: vi.fn(),
    questionnaireShown: vi.fn(() => true),
  },
  mutationState: { isPending: false },
  flagState: {
    variant: "template-first" as string | boolean | undefined,
  },
  stateOverrides: { current: {} as Partial<OnboardingState> },
  clerkUser: {
    current: { createdAt: new Date("2026-08-28T00:00:00Z") } as {
      createdAt: Date | null;
    } | null,
  },
}));

const baseState: OnboardingState = {
  tour_completed_at: null,
  modal_dismissed_at: null,
  first_save_at: null,
  first_run_at: null,
  ab_variant: "template-first",
  user_intent: null,
  questionnaire_prompted_at: null,
  seen_canvas: null,
  seen_node_adder: null,
  seen_sidebar: null,
  seen_save_run: null,
};

const completedQuestionnaire = {
  version: 1,
  response_id: "response",
  revision: 1,
  last_mutation_id: "mutation",
  status: "completed",
  role: "developer",
  company_context: "startup",
  scale_intent: "exploring",
  referral_source: "search",
  completed_at: "2026-01-01T00:00:00Z",
  skipped_at: null,
  deferred_at: null,
  updated_at: "2026-01-01T00:00:00Z",
  defer_prompt_count: 0,
} satisfies QuestionnaireStateV1;

vi.mock("posthog-js/react", () => ({
  useFeatureFlagVariantKey: () => flagState.variant,
}));
vi.mock("@clerk/clerk-react", () => ({
  useAuth: () => ({ userId: "user-a" }),
  useUser: () => ({ isLoaded: true, user: clerkUser.current }),
}));
vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: vi.fn(),
}));

vi.mock("@/store/onboarding/useOnboardingState", () => ({
  useOnboardingState: () => ({
    state: {
      ...baseState,
      ...stateOverrides.current,
      ab_variant: flagState.variant,
    },
    isNewUser: true,
    updateState: mockUpdateState,
    updateStateConfirmed: mockUpdateStateConfirmed,
  }),
}));

vi.mock("./CopilotCTAStep", async () => {
  const { useEffect } = await import("react");
  return {
    CopilotCTAStep: (props: {
      onSkip: () => void;
      onBusyChange?: (busy: boolean) => void;
    }) => {
      useEffect(() => {
        props.onBusyChange?.(true);
      }, [props]);
      return (
        <>
          <div>copilot-header</div>
          <div>copilot-body</div>
          <div>
            <button type="button" onClick={props.onSkip}>
              child-skip
            </button>
          </div>
        </>
      );
    },
  };
});

vi.mock("@/routes/workflows/hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [], isLoading: false }),
}));

vi.mock("@/routes/workflows/hooks/useCreateWorkflowMutation", () => ({
  useCreateWorkflowMutation: () => ({
    mutate: mockMutate,
    isPending: mutationState.isPending,
  }),
}));

vi.mock("@/util/onboarding/OnboardingTelemetry", () => ({
  OnboardingTelemetry: mockTelemetry,
}));

import {
  DECIDING_PLACEHOLDER_DELAY_MS,
  GetStartedModal,
} from "./GetStartedModal";

beforeEach(() => {
  mockUpdateStateConfirmed.mockResolvedValue({
    onboarding_state: { ...baseState, ...stateOverrides.current },
    launch_date_at_signup: "2026-08-27T00:00:00Z",
    recovery_guidance_assignment: null,
    questionnaire_prompt_result: { status: "flag_disabled" },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mutationState.isPending = false;
  flagState.variant = "template-first";
  stateOverrides.current = {};
  clerkUser.current = { createdAt: new Date("2026-08-28T00:00:00Z") };
});

async function renderTemplatesStep() {
  render(<GetStartedModal />, { wrapper: MemoryRouter });
  fireEvent.click(
    await screen.findByRole("button", { name: /Fill out forms/ }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
}

describe("GetStartedModal progress", () => {
  it("uses server-owned reservation state instead of a frontend questionnaire flag", async () => {
    render(<GetStartedModal />, { wrapper: MemoryRouter });
    expect(await screen.findByText("STEP 1 OF 2")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Close" })).toBeTruthy();
    expect(screen.queryByText("STEP 1 OF 3")).toBeNull();
    expect(vi.mocked(useFeatureFlag)).not.toHaveBeenCalled();
  });

  it.each([
    [undefined, "ineligible"],
    [false, "flag_disabled"],
  ] as const)(
    "renders no editor content when the editor flag is %s",
    async (variant, status) => {
      flagState.variant = variant;
      mockUpdateStateConfirmed.mockResolvedValueOnce({
        onboarding_state: baseState,
        launch_date_at_signup: "2026-08-27T00:00:00Z",
        recovery_guidance_assignment: null,
        questionnaire_prompt_result: { status },
      });
      render(<GetStartedModal />, { wrapper: MemoryRouter });

      await waitFor(() =>
        expect(mockUpdateStateConfirmed).toHaveBeenCalledOnce(),
      );
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(screen.queryByText("What do you want to automate?")).toBeNull();
      expect(screen.queryByText("Pick a template to start")).toBeNull();
      expect(screen.queryByText("copilot-header")).toBeNull();
    },
  );

  it("keeps editor-only final steps and existing responses closed", async () => {
    stateOverrides.current = {
      user_intent: "fill_forms",
      questionnaire: null,
    };
    const { unmount } = render(<GetStartedModal />, {
      wrapper: MemoryRouter,
    });

    expect(await screen.findByText("STEP 2 OF 2")).toBeTruthy();
    expect(screen.getByText("FINAL STEP")).toBeTruthy();
    expect(screen.queryByText("OPTIONAL")).toBeNull();
    unmount();
    mockUpdateStateConfirmed.mockClear();

    stateOverrides.current = {
      user_intent: "fill_forms",
      questionnaire: completedQuestionnaire,
    };
    render(<GetStartedModal />, { wrapper: MemoryRouter });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mockUpdateStateConfirmed).not.toHaveBeenCalled();
  });

  it("marks the editor second step as final", async () => {
    await renderTemplatesStep();
    expect(screen.getByText("STEP 2 OF 2")).toBeTruthy();
    expect(screen.getByText("FINAL STEP")).toBeTruthy();
  });
});

describe("GetStartedModal pre-cutoff users", () => {
  it("shows nothing and never calls the reserve endpoint for a pre-cutoff user with a prior save", async () => {
    clerkUser.current = { createdAt: new Date("2026-08-01T00:00:00Z") };
    stateOverrides.current = { first_save_at: "2026-08-20T00:00:00Z" };
    render(<GetStartedModal />, { wrapper: MemoryRouter });

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mockUpdateStateConfirmed).not.toHaveBeenCalled();
  });

  it("routes a pre-cutoff new user straight to the editor without a reserve call", async () => {
    clerkUser.current = { createdAt: new Date("2026-08-01T00:00:00Z") };
    render(<GetStartedModal />, { wrapper: MemoryRouter });

    expect(
      await screen.findByText("What do you want to automate?"),
    ).toBeTruthy();
    expect(mockUpdateStateConfirmed).not.toHaveBeenCalled();
  });
});

describe("GetStartedModal deciding placeholder", () => {
  it("closes without ever showing the placeholder when there is nothing to show", async () => {
    flagState.variant = undefined;
    let resolveReservation!: (value: ConfirmedWriteResult) => void;
    mockUpdateStateConfirmed.mockReturnValueOnce(
      new Promise<ConfirmedWriteResult>((resolve) => {
        resolveReservation = resolve;
      }),
    );
    render(<GetStartedModal />, { wrapper: MemoryRouter });

    const dialog = await screen.findByRole("dialog");
    expect(screen.getByText("Checking your onboarding setup.")).toBeTruthy();
    expect(dialog.className).toContain("opacity-0");
    expect((dialog.previousElementSibling as HTMLElement).className).toContain(
      "opacity-0",
    );

    await act(async () =>
      resolveReservation({
        onboarding_state: baseState,
        launch_date_at_signup: "2026-08-27T00:00:00Z",
        recovery_guidance_assignment: null,
        questionnaire_prompt_result: { status: "ineligible" },
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("reveals the placeholder only after the grace delay while still deciding", async () => {
    vi.useFakeTimers();
    try {
      mockUpdateStateConfirmed.mockReturnValueOnce(
        new Promise<ConfirmedWriteResult>(() => {}),
      );
      render(<GetStartedModal />, { wrapper: MemoryRouter });

      const dialog = screen.getByRole("dialog");
      expect(dialog.className).toContain("opacity-0");

      act(() => {
        vi.advanceTimersByTime(DECIDING_PLACEHOLDER_DELAY_MS);
      });
      expect(dialog.className).not.toContain("opacity-0");
      expect(
        (dialog.previousElementSibling as HTMLElement).className,
      ).not.toContain("opacity-0");
      expect(screen.getByText("Checking your onboarding setup.")).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("GetStartedModal skip-while-creating guard", () => {
  it("blocks Skip while a template workflow creation is in flight", async () => {
    mutationState.isPending = true;
    await renderTemplatesStep();
    const skip = screen.getByRole("button", { name: "Skip" });
    expect((skip as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(skip);
    expect(mockTelemetry.modalSkipped).not.toHaveBeenCalled();
    expect(mockUpdateState).not.toHaveBeenCalledWith(
      expect.objectContaining({ modal_dismissed_at: expect.any(String) }),
    );
  });

  it("dismisses on Skip when no creation is pending", async () => {
    mutationState.isPending = false;
    await renderTemplatesStep();
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    expect(mockTelemetry.modalSkipped).toHaveBeenCalledTimes(1);
    expect(mockUpdateState).toHaveBeenCalledWith(
      expect.objectContaining({ modal_dismissed_at: expect.any(String) }),
    );
  });

  it("ignores skip and Escape-close while the copilot step reports busy", async () => {
    flagState.variant = "copilot-first";
    await renderTemplatesStep();
    const chrome = screen.getByTestId("copilot-chrome");
    expect(chrome.children).toHaveLength(3);
    expect(screen.getByText("STEP 2 OF 2")).toBeTruthy();
    expect(screen.getByText("FINAL STEP")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "child-skip" }));
    fireEvent.keyDown(document, { key: "Escape" });

    expect(mockTelemetry.modalSkipped).not.toHaveBeenCalled();
    expect(mockUpdateState).not.toHaveBeenCalledWith(
      expect.objectContaining({ modal_dismissed_at: expect.any(String) }),
    );
  });
});
