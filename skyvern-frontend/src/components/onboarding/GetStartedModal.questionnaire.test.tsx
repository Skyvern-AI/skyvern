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
  LegacyOnboardingStatePatch,
  OnboardingState,
  OnboardingStateResponse,
  QuestionnairePatchV1,
  QuestionnaireStateV1,
} from "@/store/onboarding/types";
import { OnboardingContext } from "@/store/onboarding/useOnboardingState";
import { GetStartedModal } from "./GetStartedModal";

const mocks = vi.hoisted(() => ({
  userId: "user-a",
  confirmed: vi.fn<(patch: ConfirmedPatch) => Promise<ConfirmedWriteResult>>(),
  createWorkflow: vi.fn(),
  legacyUpdate: vi.fn<(patch: LegacyOnboardingStatePatch) => void>(),
  telemetry: {
    registerVariant: vi.fn(),
    flowStarted: vi.fn(),
    modalOpened: vi.fn(),
    modalSkipped: vi.fn(),
    questionnaireShown: vi.fn<(input: unknown) => boolean>(() => true),
    questionnaireCompleted: vi.fn(),
    questionnaireSkipped: vi.fn(),
    questionnaireUpdated: vi.fn(),
  },
}));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagVariantKey: () => "template-first",
}));
vi.mock("@clerk/clerk-react", () => ({
  useAuth: () => ({ userId: mocks.userId }),
  useUser: () => ({
    isLoaded: true,
    user: { createdAt: new Date("2026-08-28T00:00:00Z") },
  }),
}));
vi.mock("@/routes/workflows/hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [], isLoading: false }),
}));
vi.mock("@/routes/workflows/hooks/useCreateWorkflowMutation", () => ({
  useCreateWorkflowMutation: () => ({
    mutate: mocks.createWorkflow,
    isPending: false,
  }),
}));
vi.mock("@/util/onboarding/OnboardingTelemetry", () => ({
  OnboardingTelemetry: mocks.telemetry,
}));
vi.mock("./CopilotCTAStep", () => ({
  CopilotCTAStep: () => null,
}));
vi.mock("./QuestionnaireDetailsStep", () => ({
  QuestionnaireDetailsStep: ({
    completionAction,
    expectedRevision,
    externalError,
    isPending,
    onAction,
    onBack,
  }: {
    completionAction: "complete" | "update";
    expectedRevision: number;
    externalError?: string | null;
    isPending: boolean;
    onAction: (patch: QuestionnairePatchV1) => Promise<void>;
    onBack: () => void;
  }) => {
    const answerPatch = {
      version: 1 as const,
      mutation_id: `mutation-${completionAction}-${expectedRevision}`,
      expected_revision: expectedRevision,
      action: completionAction,
      role: "developer" as const,
      company_context: "startup" as const,
      scale_intent: "exploring" as const,
      referral_source: "search" as const,
    };
    return (
      <div>
        <span>{`details-${completionAction}-${expectedRevision}`}</span>
        {externalError ? <div role="alert">{externalError}</div> : null}
        <button
          type="button"
          disabled={isPending}
          onClick={() => void onAction(answerPatch)}
        >
          details-submit
        </button>
        <button type="button" onClick={onBack}>
          details-back
        </button>
      </div>
    );
  },
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

function questionnaire(
  overrides: Partial<QuestionnaireStateV1> = {},
): QuestionnaireStateV1 {
  return {
    version: 1,
    response_id: "response",
    revision: 1,
    last_mutation_id: "mutation-complete-0",
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
    ...overrides,
  };
}

function response(
  overrides: Partial<OnboardingState> = {},
): OnboardingStateResponse {
  return {
    onboarding_state: { ...baseState, ...overrides },
    launch_date_at_signup: "2026-01-01T00:00:00Z",
    recovery_guidance_assignment: null,
  };
}

const promptedAt = "2026-08-27T00:00:00Z";
const reservedResponse = (overrides: Partial<OnboardingState> = {}) => ({
  ...response({ ...overrides, questionnaire_prompted_at: promptedAt }),
  questionnaire_prompt_result: {
    status: "reserved" as const,
    prompted_at: promptedAt,
  },
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function TestModal({
  initialState = baseState,
  editorState,
}: {
  initialState?: OnboardingState;
  editorState?: Partial<
    Pick<
      OnboardingState,
      | "modal_dismissed_at"
      | "first_save_at"
      | "questionnaire_prompted_at"
      | "questionnaire"
    >
  >;
}) {
  const [state, setState] = useState(initialState);
  function updateState(patch: LegacyOnboardingStatePatch) {
    mocks.legacyUpdate(patch);
    setState((current) => ({
      ...current,
      ...patch,
      questionnaire: current.questionnaire,
    }));
  }
  async function updateStateConfirmed(patch: ConfirmedPatch) {
    const next = await mocks.confirmed(patch);
    if ("onboarding_state" in next) {
      setState(next.onboarding_state);
    }
    return next;
  }
  return (
    <MemoryRouter>
      <OnboardingContext.Provider
        value={{
          state: editorState ? { ...state, ...editorState } : state,
          isLoading: false,
          updateState,
          updateStateConfirmed,
          isNewUser: true,
          abVariant: "template-first",
          recoveryGuidanceAssignment: null,
        }}
      >
        <GetStartedModal />
      </OnboardingContext.Provider>
    </MemoryRouter>
  );
}

beforeEach(() => {
  mocks.confirmed.mockResolvedValue(response({ user_intent: "fill_forms" }));
  mocks.telemetry.questionnaireShown.mockReturnValue(true);
  mocks.userId = "user-a";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("GetStartedModal", () => {
  it("reserves once, completes, stamps dismissal, and closes", async () => {
    const reservation = deferred<ConfirmedWriteResult>();
    const completion = deferred<OnboardingStateResponse>();
    mocks.confirmed
      .mockReturnValueOnce(reservation.promise)
      .mockResolvedValueOnce(
        response({
          user_intent: "fill_forms",
          questionnaire_prompted_at: promptedAt,
        }),
      )
      .mockReturnValueOnce(completion.promise);
    const view = render(
      <StrictMode>
        <TestModal />
      </StrictMode>,
    );
    await waitFor(() => expect(mocks.confirmed).toHaveBeenCalledOnce());
    view.rerender(
      <StrictMode>
        <TestModal />
      </StrictMode>,
    );
    expect(mocks.confirmed).toHaveBeenCalledWith({
      questionnaire_prompt: { version: 1, action: "reserve" },
    });
    await act(async () => reservation.resolve(reservedResponse()));
    expect(mocks.telemetry.questionnaireShown).toHaveBeenCalledWith(
      expect.objectContaining({ primaryIntent: null, promptReason: "initial" }),
    );
    expect(screen.getByText("STEP 1 OF 2")).toBeTruthy();
    fireEvent.click(
      await screen.findByRole("button", { name: /Fill out forms/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.click(await screen.findByText("details-submit"));
    await waitFor(() => expect(mocks.confirmed).toHaveBeenCalledTimes(3));
    expect(mocks.telemetry.questionnaireCompleted).not.toHaveBeenCalled();
    fireEvent.keyDown(document, { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(mocks.confirmed).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("dialog")).toBeTruthy();
    await act(async () =>
      completion.resolve(
        response({
          user_intent: "fill_forms",
          questionnaire_prompted_at: promptedAt,
          questionnaire: questionnaire(),
        }),
      ),
    );
    expect(mocks.telemetry.questionnaireCompleted).toHaveBeenCalledOnce();
    expect(mocks.confirmed).toHaveBeenCalledTimes(3);
    expect(mocks.legacyUpdate).toHaveBeenCalledWith({
      modal_dismissed_at: expect.any(String),
    });
    expect(mocks.telemetry.modalSkipped).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("closes when Intent Continue first sees a foreign response", async () => {
    mocks.confirmed
      .mockResolvedValueOnce(reservedResponse())
      .mockResolvedValueOnce(
        response({
          user_intent: "fill_forms",
          questionnaire_prompted_at: promptedAt,
          questionnaire: questionnaire({ response_id: "foreign-response" }),
        }),
      );
    render(<TestModal />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Fill out forms/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mocks.confirmed).toHaveBeenCalledTimes(2);
  });

  it("reserves and opens the questionnaire for an initially dismissed editor", async () => {
    const dismissedAt = "2026-01-01T00:00:00Z";
    mocks.confirmed.mockResolvedValueOnce(
      reservedResponse({ modal_dismissed_at: dismissedAt }),
    );
    render(
      <TestModal
        initialState={{ ...baseState, modal_dismissed_at: dismissedAt }}
      />,
    );
    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(mocks.confirmed).toHaveBeenCalledOnce();
    expect(mocks.telemetry.questionnaireShown).toHaveBeenCalledOnce();
  });

  it("opens the reserved questionnaire when sessionStorage.getItem throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    mocks.confirmed.mockResolvedValueOnce(reservedResponse());
    render(<TestModal />);
    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(mocks.telemetry.questionnaireShown).toHaveBeenCalledOnce();
  });

  it("opens the reserved questionnaire when sessionStorage.setItem throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockReturnValue(null);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    mocks.confirmed.mockResolvedValueOnce(reservedResponse());
    render(<TestModal />);
    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(mocks.telemetry.questionnaireShown).toHaveBeenCalledOnce();
  });

  it("never reopens a legacy deferred response", async () => {
    const deferredState = questionnaire({
      status: "deferred",
      completed_at: null,
      deferred_at: "2026-01-01T00:00:00Z",
      defer_prompt_count: 1,
    });
    const props = {
      initialState: {
        ...baseState,
        user_intent: "fill_forms",
        questionnaire: deferredState,
      },
    };
    render(<TestModal {...props} />);
    await act(async () => undefined);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mocks.confirmed).not.toHaveBeenCalled();
  });

  it("keeps initial details open and announces a failed dialog close", async () => {
    const closeWrite = deferred<ConfirmedWriteResult>();
    mocks.confirmed
      .mockResolvedValueOnce(reservedResponse())
      .mockResolvedValueOnce(
        response({
          user_intent: "fill_forms",
          questionnaire_prompted_at: promptedAt,
        }),
      )
      .mockReturnValueOnce(closeWrite.promise);
    render(<TestModal />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Fill out forms/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("details-complete-0")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(mocks.confirmed).toHaveBeenCalledTimes(3));
    expect(screen.getByText("details-complete-0")).toBeTruthy();
    await act(async () => closeWrite.reject(new Error("offline")));

    expect((await screen.findByRole("alert")).textContent).toBe(
      "We couldn't save your choice. Try again.",
    );
    expect(screen.getByText("details-complete-0")).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("persists initial Skip before telemetry and closing", async () => {
    const write = deferred<OnboardingStateResponse>();
    mocks.confirmed
      .mockResolvedValueOnce(reservedResponse())
      .mockReturnValueOnce(write.promise);
    render(<TestModal />);
    fireEvent.click(await screen.findByRole("button", { name: "Skip" }));
    await waitFor(() => expect(mocks.confirmed).toHaveBeenCalledTimes(2));
    const skipPatch =
      mocks.confirmed.mock.calls[mocks.confirmed.mock.calls.length - 1]?.[0]
        .questionnaire;
    expect(skipPatch).toEqual(
      expect.objectContaining({ expected_revision: 0, action: "skip" }),
    );
    if (!skipPatch) throw new Error("missing Skip patch");
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(mocks.telemetry.questionnaireSkipped).not.toHaveBeenCalled();
    await act(async () =>
      write.resolve(
        response({
          questionnaire_prompted_at: promptedAt,
          questionnaire: questionnaire({
            status: "skipped",
            completed_at: null,
            skipped_at: promptedAt,
            last_mutation_id: skipPatch.mutation_id,
          }),
        }),
      ),
    );
    expect(mocks.telemetry.questionnaireSkipped).toHaveBeenCalledWith(
      expect.objectContaining({ responseId: "response", revision: 1 }),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("ignores a reservation result after unmount", async () => {
    const write = deferred<ConfirmedWriteResult>();
    mocks.confirmed.mockReturnValueOnce(write.promise);
    const view = render(<TestModal />);
    await waitFor(() => expect(mocks.confirmed).toHaveBeenCalledOnce());
    expect(screen.getByRole("dialog")).toBeTruthy();

    view.unmount();
    await act(async () => write.resolve(reservedResponse()));

    expect(mocks.telemetry.questionnaireShown).not.toHaveBeenCalled();
  });

  it("resets reservation ownership when the Clerk user changes", async () => {
    const firstUserReservation = deferred<ConfirmedWriteResult>();
    mocks.confirmed
      .mockReturnValueOnce(firstUserReservation.promise)
      .mockResolvedValueOnce(reservedResponse());
    const view = render(<TestModal />);
    await waitFor(() => expect(mocks.confirmed).toHaveBeenCalledOnce());

    mocks.userId = "user-b";
    view.rerender(<TestModal />);
    await waitFor(() => expect(mocks.confirmed).toHaveBeenCalledTimes(2));
    expect(
      await screen.findByText("What do you want to automate?"),
    ).toBeTruthy();

    await act(async () => firstUserReservation.resolve(reservedResponse()));
    expect(mocks.telemetry.questionnaireShown).toHaveBeenCalledOnce();
  });

  it("closes an ineligible fallback after another tab reserves", async () => {
    const write = deferred<ConfirmedWriteResult>();
    mocks.confirmed.mockReturnValueOnce(write.promise);
    const view = render(<TestModal />);
    await waitFor(() => expect(mocks.confirmed).toHaveBeenCalledOnce());
    view.rerender(
      <TestModal editorState={{ questionnaire_prompted_at: promptedAt }} />,
    );

    await act(async () =>
      write.resolve({
        ...response(),
        questionnaire_prompt_result: { status: "ineligible" },
      }),
    );

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText("Pick a template to start")).toBeNull();
    expect(mocks.telemetry.questionnaireShown).not.toHaveBeenCalled();
  });

  it.each(["modal_dismissed_at", "first_save_at"] as const)(
    "ignores a stale reservation when %s changes while pending",
    async (field) => {
      const write = deferred<ConfirmedWriteResult>();
      mocks.confirmed.mockReturnValueOnce(write.promise);
      const view = render(<TestModal />);
      await waitFor(() => expect(mocks.confirmed).toHaveBeenCalledOnce());
      view.rerender(
        <TestModal editorState={{ [field]: "2026-01-01T00:00:00Z" }} />,
      );
      await act(async () => write.resolve(reservedResponse()));
      expect(mocks.telemetry.questionnaireShown).not.toHaveBeenCalled();
      expect(screen.queryByRole("dialog")).toBeNull();
    },
  );
});
