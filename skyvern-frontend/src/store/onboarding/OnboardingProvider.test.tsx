// @vitest-environment jsdom
import { useState } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  OnboardingState,
  OnboardingStateResponse,
  RecoveryGuidanceAssignment,
  QuestionnairePatchV1,
  QuestionnaireStateV1,
} from "./types";

const { mockGet, mockPost, mockAuth } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockAuth: { userId: "user-a" },
}));

vi.mock("@clerk/clerk-react", () => ({
  useAuth: () => ({ isSignedIn: true, userId: mockAuth.userId }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));
vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ get: mockGet, post: mockPost }),
}));
vi.mock("@/util/onboarding/OnboardingTelemetry", () => ({
  OnboardingTelemetry: {
    error: vi.fn(),
    registerVariant: vi.fn(),
    firstWorkflowCreated: vi.fn(),
    firstRunCompleted: vi.fn(),
  },
}));

type BroadcastListener = (event: MessageEvent<unknown>) => void;

class FakeBroadcastChannel {
  static channels: FakeBroadcastChannel[] = [];

  readonly name: string;
  private readonly listeners = new Set<BroadcastListener>();

  constructor(name: string) {
    this.name = name;
    FakeBroadcastChannel.channels.push(this);
  }

  postMessage(data: unknown) {
    for (const channel of FakeBroadcastChannel.channels) {
      if (channel !== this && channel.name === this.name) {
        for (const listener of channel.listeners) {
          listener(new MessageEvent("message", { data }));
        }
      }
    }
  }

  addEventListener(_type: "message", listener: BroadcastListener) {
    this.listeners.add(listener);
  }

  removeEventListener(_type: "message", listener: BroadcastListener) {
    this.listeners.delete(listener);
  }

  close() {
    FakeBroadcastChannel.channels = FakeBroadcastChannel.channels.filter(
      (channel) => channel !== this,
    );
  }
}

import { OnboardingProvider } from "./OnboardingProvider";
import { useOnboardingState } from "./useOnboardingState";

const DISMISSED_AT = "2026-01-01T00:00:00.000Z";
const DISMISSED_LATER = "2026-01-02T00:00:00.000Z";
const RECOVERY_GUIDANCE_ASSIGNMENT = {
  experiment_version: "recovery-guidance-v1",
  organization_id: "organization",
  eligible_run_id: "run",
  eligible_at: "2026-01-01T00:00:00Z",
  arm: "treatment",
} satisfies RecoveryGuidanceAssignment;
const COMPLETE: QuestionnairePatchV1 = {
  version: 1,
  mutation_id: "mutation",
  expected_revision: 0,
  action: "complete",
  role: "developer",
  company_context: "startup",
  scale_intent: "exploring",
  referral_source: "search",
};

function questionnaire(revision = 1): QuestionnaireStateV1 {
  return {
    version: 1,
    response_id: "response",
    revision,
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
  };
}

function response(
  overrides: Partial<OnboardingState> = {},
): OnboardingStateResponse {
  return {
    onboarding_state: {
      tour_completed_at: null,
      modal_dismissed_at: null,
      first_save_at: null,
      first_run_at: null,
      ab_variant: null,
      user_intent: "fill_forms",
      seen_canvas: null,
      seen_node_adder: null,
      seen_sidebar: null,
      seen_save_run: null,
      ...overrides,
    },
    launch_date_at_signup: "2025-01-01T00:00:00Z",
    recovery_guidance_assignment: null,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function Consumer({ prefix = "" }: Readonly<{ prefix?: string }>) {
  const {
    state,
    updateState,
    updateStateConfirmed,
    recoveryGuidanceAssignment,
  } = useOnboardingState();
  const [confirmed, setConfirmed] = useState("idle");
  return (
    <div>
      <span data-testid={`${prefix}dismissed`}>
        {String(state?.modal_dismissed_at)}
      </span>
      <span data-testid={`${prefix}revision`}>
        {state?.questionnaire?.revision ?? 0}
      </span>
      <span data-testid={`${prefix}prompted`}>
        {String(state?.questionnaire_prompted_at)}
      </span>
      <span data-testid={`${prefix}seen`}>{String(state?.seen_canvas)}</span>
      <span data-testid={`${prefix}confirmed`}>{confirmed}</span>
      <span data-testid={`${prefix}assignment`}>
        {recoveryGuidanceAssignment?.eligible_run_id ?? "none"}
      </span>
      <button
        type="button"
        onClick={() => updateState({ modal_dismissed_at: DISMISSED_AT })}
      >
        dismiss
      </button>
      <button
        type="button"
        onClick={() => updateState({ modal_dismissed_at: DISMISSED_LATER })}
      >
        dismiss later
      </button>
      <button type="button" onClick={() => updateState({ seen_canvas: true })}>
        see canvas
      </button>
      <button
        type="button"
        onClick={() => {
          void updateStateConfirmed({ questionnaire: COMPLETE }).then(
            (result) =>
              setConfirmed(
                "code" in result && typeof result.code === "string"
                  ? result.code
                  : "saved",
              ),
            () => setConfirmed("thrown"),
          );
        }}
      >
        {prefix}confirm
      </button>
      <button
        type="button"
        onClick={() => {
          void updateStateConfirmed({
            questionnaire_prompt: { version: 1, action: "reserve" },
          });
        }}
      >
        {prefix}reserve
      </button>
    </div>
  );
}

function renderProvider() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 5 * 60 * 1000 },
      mutations: { retry: false },
    },
  });
  const renderTree = () => (
    <QueryClientProvider client={queryClient}>
      <OnboardingProvider>
        <Consumer />
      </OnboardingProvider>
    </QueryClientProvider>
  );
  const rendered = render(renderTree());
  return {
    ...rendered,
    queryClient,
    rerenderProvider: () => rendered.rerender(renderTree()),
  };
}

function renderProviderPair() {
  const firstClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 5 * 60 * 1000 },
      mutations: { retry: false },
    },
  });
  const secondClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 5 * 60 * 1000 },
      mutations: { retry: false },
    },
  });
  return render(
    <>
      <QueryClientProvider client={firstClient}>
        <OnboardingProvider>
          <Consumer prefix="a-" />
        </OnboardingProvider>
      </QueryClientProvider>
      <QueryClientProvider client={secondClient}>
        <OnboardingProvider>
          <Consumer prefix="b-" />
        </OnboardingProvider>
      </QueryClientProvider>
    </>,
  );
}

beforeEach(() => {
  FakeBroadcastChannel.channels = [];
  vi.stubGlobal("BroadcastChannel", FakeBroadcastChannel);
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  vi.unstubAllGlobals();
  FakeBroadcastChannel.channels = [];
  mockAuth.userId = "user-a";
});

describe("OnboardingProvider writes", () => {
  it("keeps confirmed POST success when the background refetch fails", async () => {
    const confirmed = response({ questionnaire: questionnaire() });
    mockGet
      .mockResolvedValueOnce({ data: response() })
      .mockRejectedValueOnce(new Error("refetch unavailable"));
    mockPost.mockResolvedValueOnce({ data: confirmed });
    renderProvider();
    await waitFor(() => expect(mockGet).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByText("confirm"));

    await waitFor(() =>
      expect(screen.getByTestId("confirmed").textContent).toBe("saved"),
    );
    expect(screen.getByTestId("revision").textContent).toBe("1");
  });

  it("broadcasts a reservation into another tab immediately", async () => {
    const backgroundRefetch = deferred<{ data: OnboardingStateResponse }>();
    mockGet
      .mockResolvedValueOnce({ data: response() })
      .mockResolvedValueOnce({ data: response() })
      .mockReturnValue(backgroundRefetch.promise);
    const reserved = response({
      questionnaire_prompted_at: "2026-08-28T00:30:00Z",
    });
    reserved.questionnaire_prompt_result = {
      status: "reserved",
      prompted_at: "2026-08-28T00:30:00Z",
    };
    mockPost.mockResolvedValueOnce({ data: reserved });
    renderProviderPair();
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByText("b-reserve"));

    await waitFor(() =>
      expect(screen.getByTestId("a-prompted").textContent).toBe(
        "2026-08-28T00:30:00Z",
      ),
    );
  });
  it("preserves one failed overlay through a synchronized POST/refetch interleaving", async () => {
    const firstWrite = deferred<{ data: OnboardingStateResponse }>();
    mockGet
      .mockResolvedValueOnce({ data: response() })
      .mockReturnValueOnce(firstWrite.promise)
      .mockResolvedValue({ data: response({ seen_canvas: true }) });
    mockPost
      .mockReturnValueOnce(firstWrite.promise)
      .mockRejectedValueOnce(new Error("offline"));
    const { queryClient } = renderProvider();
    await waitFor(() => expect(mockGet).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByText("see canvas"));
    await waitFor(() => expect(mockPost).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByText("dismiss"));
    await waitFor(() =>
      expect(screen.getByTestId("dismissed").textContent).toBe(DISMISSED_AT),
    );
    void queryClient.invalidateQueries({ queryKey: ["userOnboarding"] });
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));

    firstWrite.resolve({ data: response({ seen_canvas: true }) });
    await waitFor(() =>
      expect([queryClient.isMutating(), queryClient.isFetching()]).toEqual([
        0, 0,
      ]),
    );
    expect(screen.getByTestId("dismissed").textContent).toBe(DISMISSED_AT);
  });

  it("lets a later successful field value supersede an older failure", async () => {
    mockGet
      .mockResolvedValueOnce({ data: response() })
      .mockResolvedValueOnce({
        data: response({ modal_dismissed_at: DISMISSED_LATER }),
      })
      .mockResolvedValue({
        data: response({
          modal_dismissed_at: DISMISSED_LATER,
          seen_canvas: true,
        }),
      });
    mockPost
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({
        data: response({ modal_dismissed_at: DISMISSED_LATER }),
      })
      .mockResolvedValueOnce({
        data: response({
          modal_dismissed_at: DISMISSED_LATER,
          seen_canvas: true,
        }),
      });
    renderProvider();
    await waitFor(() => expect(mockGet).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByText("dismiss"));
    await waitFor(() => expect(mockPost).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByText("dismiss later"));
    await waitFor(() =>
      expect(screen.getByTestId("dismissed").textContent).toBe(DISMISSED_LATER),
    );

    fireEvent.click(screen.getByText("see canvas"));
    await waitFor(() =>
      expect(screen.getByTestId("seen").textContent).toBe("true"),
    );
    expect(screen.getByTestId("dismissed").textContent).toBe(DISMISSED_LATER);
  });

  it("keeps the GET-only recovery assignment after a confirmed write", async () => {
    const assigned = (overrides: Partial<OnboardingState> = {}) => {
      const value = response(overrides);
      value.recovery_guidance_assignment = RECOVERY_GUIDANCE_ASSIGNMENT;
      return value;
    };
    const confirmed = response({ questionnaire: questionnaire() });
    Reflect.deleteProperty(confirmed, "recovery_guidance_assignment");
    mockGet
      .mockResolvedValueOnce({ data: assigned() })
      .mockResolvedValueOnce({ data: assigned({ seen_canvas: true }) })
      .mockResolvedValueOnce({
        data: assigned({ questionnaire: questionnaire(), seen_canvas: true }),
      });
    mockPost
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ data: confirmed });
    const { queryClient } = renderProvider();
    fireEvent.click(screen.getByText("dismiss"));
    await waitFor(() => expect(queryClient.isMutating()).toBe(0));
    await act(() =>
      queryClient.invalidateQueries({ queryKey: ["userOnboarding"] }),
    );
    fireEvent.click(screen.getByText("confirm"));
    await waitFor(() =>
      expect(screen.getByTestId("revision").textContent).toBe("1"),
    );
    expect(screen.getByTestId("confirmed").textContent).toBe("saved");
    expect(screen.getByTestId("assignment").textContent).toBe("run");
  });

  it("rejects a confirmed conflict when authoritative reconciliation fails", async () => {
    const conflict = Object.assign(
      new Error("Request failed with status code 409"),
      {
        isAxiosError: true,
        response: {
          status: 409,
          data: { detail: "questionnaire_revision_conflict" },
        },
      },
    );
    mockGet
      .mockResolvedValueOnce({ data: response() })
      .mockRejectedValueOnce(new Error("reconciliation unavailable"));
    mockPost.mockRejectedValueOnce(conflict);
    renderProvider();
    await waitFor(() => expect(mockGet).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByText("confirm"));

    await waitFor(() =>
      expect(screen.getByTestId("confirmed").textContent).toBe("thrown"),
    );
  });

  it("isolates B's failed overlay while A settles after the switch", async () => {
    const aWrite = deferred<{ data: OnboardingStateResponse }>();
    const aRefetch = deferred<{ data: OnboardingStateResponse }>();
    const bWrite = deferred<void>();
    mockGet
      .mockResolvedValueOnce({ data: response() })
      .mockReturnValueOnce(aRefetch.promise)
      .mockResolvedValue({ data: response() });
    mockPost.mockReturnValueOnce(aWrite.promise).mockReturnValueOnce(
      bWrite.promise.then(() => {
        throw new Error("offline");
      }),
    );
    const { queryClient, rerenderProvider } = renderProvider();
    const invalidate = () =>
      queryClient.invalidateQueries({ queryKey: ["userOnboarding"] });
    await waitFor(() => expect(mockGet).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByText("see canvas"));
    await waitFor(() => expect(mockPost).toHaveBeenCalledOnce());
    const aRefresh = invalidate();
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    mockAuth.userId = "user-b";
    rerenderProvider();
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect(screen.getByTestId("dismissed").textContent).toBe("null"),
    );
    fireEvent.click(screen.getByText("dismiss"));
    await waitFor(() =>
      expect(screen.getByTestId("dismissed").textContent).toBe(DISMISSED_AT),
    );
    aRefetch.resolve({ data: response() });
    await aRefresh;
    expect(
      queryClient.getQueryData<OnboardingStateResponse>([
        "userOnboarding",
        "user-a",
      ])?.onboarding_state.modal_dismissed_at,
    ).toBeNull();
    aWrite.resolve({ data: response({ seen_canvas: true }) });
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
    await act(invalidate);
    expect(screen.getByTestId("dismissed").textContent).toBe(DISMISSED_AT);
    bWrite.resolve();
    await waitFor(() => expect(queryClient.isMutating()).toBe(0));
    await act(invalidate);
    expect(screen.getByTestId("dismissed").textContent).toBe(DISMISSED_AT);
  });

  it.each([
    { status: 409, detail: "questionnaire_revision_conflict" },
    { status: 409, detail: "questionnaire_requires_user_intent" },
    { status: 409, detail: "questionnaire_update_requires_response" },
    { status: 409, detail: "questionnaire_invalid_transition" },
    { status: 403, detail: "onboarding_questionnaire_disabled" },
    { status: 409, detail: undefined },
  ])(
    "returns $detail after reconciling HTTP $status",
    async ({ status, detail }) => {
      const code = detail ?? "unknown";
      const authoritative = deferred<{ data: OnboardingStateResponse }>();
      const error = Object.assign(new Error(code), {
        isAxiosError: true,
        response: {
          status,
          data: detail === undefined ? undefined : { detail },
        },
      });
      mockGet
        .mockResolvedValueOnce({ data: response() })
        .mockReturnValue(authoritative.promise);
      mockPost
        .mockRejectedValueOnce(new Error("offline"))
        .mockRejectedValueOnce(error);
      renderProvider();
      fireEvent.click(screen.getByText("dismiss"));
      await waitFor(() => expect(mockPost).toHaveBeenCalledOnce());
      fireEvent.click(screen.getByText("confirm"));

      if (status === 409) {
        authoritative.resolve({
          data: response({ questionnaire: questionnaire(2) }),
        });
        await waitFor(() =>
          expect(screen.getByTestId("revision").textContent).toBe("2"),
        );
        expect(screen.getByTestId("dismissed").textContent).toBe(DISMISSED_AT);
      }

      await waitFor(() =>
        expect(screen.getByTestId("confirmed").textContent).toBe(code),
      );
    },
  );
});
