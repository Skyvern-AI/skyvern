import { useState, type ReactNode } from "react";
import {
  QueryClient,
  QueryClientProvider,
  QueryObserver,
} from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { OnboardingProgressBand } from "@/components/onboarding/OnboardingProgressBand";
const mocks = vi.hoisted<{
  get: ReturnType<typeof vi.fn>;
  flag: boolean | undefined;
  org: string | undefined;
  user: string | undefined;
  trackRemaining: number;
  dismissHandoff: ReturnType<typeof vi.fn<() => void>>;
  trackDismissed: boolean;
  restoreTrack: ReturnType<typeof vi.fn<() => void>>;
}>(() => ({
  get: vi.fn(),
  flag: true,
  org: "org-test",
  user: "user-test",
  trackRemaining: 0,
  dismissHandoff: vi.fn<() => void>(),
  trackDismissed: false,
  restoreTrack: vi.fn<() => void>(),
}));
vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ get: mocks.get }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => mocks.flag }));
vi.mock("@/hooks/useUser", () => ({
  useUser: () => ({
    get: () => (mocks.user === undefined ? null : { id: mocks.user }),
  }),
}));
vi.mock("@/store/ActiveOrgContext", async () => ({
  ...(await vi.importActual<typeof import("@/store/ActiveOrgContext")>(
    "@/store/ActiveOrgContext",
  )),
  useActiveOrgId: () => mocks.org,
}));
import { useOnboardingProgress } from "./useOnboardingProgress";
const validPayload = () => ({
  version: "onboarding_progress_v1",
  state: "active",
  completed_count: 0,
  total_count: 2,
  next_action_key: "first_agent_created",
  items: [
    { key: "first_agent_created", completed_at: null },
    { key: "first_successful_run", completed_at: null },
  ],
});
const completedPayload = () => ({
  ...validPayload(),
  state: "completed",
  completed_count: 2,
  next_action_key: null,
  items: [
    { key: "first_agent_created", completed_at: "2026-08-20T12:00:00Z" },
    { key: "first_successful_run", completed_at: "2026-08-20T12:01:00Z" },
  ],
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
function renderProgress(client = new QueryClient()) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(() => useOnboardingProgress(), { wrapper });
  return { client, ...hook };
}
function ProgressBand() {
  const { progress, isPending, dismiss, restore } = useOnboardingProgress();
  const [trackRemaining, setTrackRemaining] = useState(mocks.trackRemaining);
  return (
    <OnboardingProgressBand
      progress={progress}
      isPending={isPending}
      onDismiss={dismiss}
      onRestore={restore}
      onDescribeAgent={vi.fn()}
      trackRemaining={trackRemaining}
      onDismissHandoff={() => {
        mocks.dismissHandoff();
        setTrackRemaining(0);
      }}
      trackDismissed={mocks.trackDismissed}
      onRestoreTrack={mocks.restoreTrack}
    />
  );
}
function renderProgressBand(client: QueryClient) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ProgressBand />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  mocks.flag = true;
  mocks.org = "org-test";
  mocks.user = "user-test";
  mocks.trackRemaining = 0;
  mocks.trackDismissed = false;
});
afterEach(() => vi.useRealTimers());
it.each([
  [false, "org-test", "user-test"],
  [undefined, "org-test", "user-test"],
  [true, undefined, "user-test"],
  [true, "org-test", undefined],
])("does not GET when onboarding progress is gated", (flag, org, user) => {
  mocks.flag = flag;
  mocks.org = org;
  mocks.user = user;
  renderProgress();
  expect(mocks.get).not.toHaveBeenCalled();
});
it.each([
  [validPayload(), "active"],
  [{ ...validPayload(), version: "onboarding_progress_v0" }, null],
])("parses valid V1 and hides malformed versions", async (data, expected) => {
  mocks.flag = true;
  mocks.org = "org-test";
  mocks.user = "user-test";
  mocks.get.mockResolvedValue({ data });
  const { result } = renderProgress();
  await waitFor(() => expect(mocks.get).toHaveBeenCalled());
  await waitFor(() =>
    expect(result.current.progress?.state ?? null).toBe(expected),
  );
});
it("refetches fresh cached progress when Discover remounts", async () => {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5 * 60 * 1_000,
      },
    },
  });
  const refreshedPayload = {
    ...validPayload(),
    completed_count: 1,
    next_action_key: "first_successful_run",
    items: [
      {
        key: "first_agent_created",
        completed_at: "2026-08-20T12:00:00Z",
      },
      { key: "first_successful_run", completed_at: null },
    ],
  };
  mocks.get
    .mockResolvedValueOnce({ data: validPayload() })
    .mockResolvedValueOnce({ data: refreshedPayload });

  const firstMount = renderProgress(client);
  await waitFor(() =>
    expect(firstMount.result.current.progress?.completed_count).toBe(0),
  );
  expect(mocks.get).toHaveBeenCalledTimes(1);
  firstMount.unmount();

  const secondMount = renderProgress(client);
  await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
  await waitFor(() =>
    expect(secondMount.result.current.progress?.completed_count).toBe(1),
  );
  expect(secondMount.result.current.progress).toEqual(refreshedPayload);
});
it("keeps one way back to a hidden track on Home", async () => {
  mocks.trackDismissed = true;
  mocks.get.mockResolvedValueOnce({ data: completedPayload() });
  renderProgressBand(new QueryClient());
  const resume = await screen.findByRole("button", {
    name: "Resume getting started",
  });
  expect(screen.queryByText(/First agent ready/)).toBeNull();
  fireEvent.click(resume);
  expect(mocks.restoreTrack).toHaveBeenCalledOnce();
});

it("keeps a persistent handoff instead of the timed celebration when the track is active", async () => {
  const client = new QueryClient();
  mocks.get.mockResolvedValueOnce({ data: validPayload() });
  const activeMount = renderProgressBand(client);
  expect(await screen.findByText("Build your first agent")).toBeTruthy();
  activeMount.unmount();

  const completedRequest = deferred<{ data: unknown }>();
  mocks.get.mockReturnValueOnce(completedRequest.promise);
  mocks.trackRemaining = 4;
  vi.useFakeTimers();
  const completionMount = renderProgressBand(client);
  completedRequest.resolve({ data: completedPayload() });
  await completedRequest.promise;
  await act(() => vi.advanceTimersByTimeAsync(0));
  const handoff = screen.getByRole("link", {
    name: "Keep going: 4 more steps →",
  });
  expect(handoff.getAttribute("href")).toBe("/getting-started");
  expect(screen.getByRole("status").textContent).toContain(
    "First agent ready.",
  );
  expect(screen.queryByText("3 of 3 complete")).toBeNull();
  act(() => vi.advanceTimersByTime(5000));
  expect(
    screen.getByRole("link", { name: "Keep going: 4 more steps →" }),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Hide" }));
  expect(mocks.dismissHandoff).toHaveBeenCalledOnce();
  expect(screen.queryByText(/First agent ready/)).toBeNull();
  act(() => vi.advanceTimersByTime(1800));
  expect(screen.queryByText(/First agent ready/)).toBeNull();
  completionMount.unmount();
  vi.useRealTimers();

  mocks.get.mockResolvedValueOnce({ data: completedPayload() });
  renderProgressBand(new QueryClient());
  expect(
    await screen.findByRole("link", { name: "Keep going: 4 more steps →" }),
  ).toBeTruthy();
  expect(screen.queryByText("3 of 3 complete")).toBeNull();
});
it("celebrates only cached active progress completing on remount", async () => {
  const freshClient = new QueryClient();
  mocks.get.mockResolvedValueOnce({ data: completedPayload() });
  const freshMount = renderProgressBand(freshClient);

  await waitFor(() => expect(mocks.get).toHaveBeenCalledOnce());
  await waitFor(() =>
    expect(
      freshClient.getQueryData([
        "onboarding-progress",
        "user-test",
        "org-test",
      ]),
    ).toEqual(completedPayload()),
  );
  expect(screen.queryByText("First agent ready")).toBeNull();
  expect(screen.queryByRole("status")).toBeNull();
  freshMount.unmount();

  const client = new QueryClient();
  mocks.get.mockResolvedValueOnce({ data: validPayload() });
  const activeMount = renderProgressBand(client);
  expect(await screen.findByText("Build your first agent")).toBeTruthy();
  activeMount.unmount();

  const completedRequest = deferred<{ data: unknown }>();
  mocks.get.mockReturnValueOnce(completedRequest.promise);
  vi.useFakeTimers();
  const completionMount = renderProgressBand(client);
  expect(screen.getByText("Build your first agent")).toBeTruthy();

  completedRequest.resolve({ data: completedPayload() });
  await completedRequest.promise;
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(screen.getAllByText("First agent ready")).toHaveLength(1);
  expect(screen.getByRole("status").textContent).toContain("3 of 3 complete");
  expect(screen.queryByRole("link", { name: /Keep going/ })).toBeNull();

  act(() => vi.advanceTimersByTime(1799));
  expect(screen.getByText("First agent ready")).toBeTruthy();
  act(() => vi.advanceTimersByTime(1));
  expect(screen.queryByText("First agent ready")).toBeNull();
  completionMount.unmount();

  const finalRequest = deferred<{ data: unknown }>();
  mocks.get.mockReturnValueOnce(finalRequest.promise);
  const finalMount = renderProgressBand(client);
  expect(finalMount.container.innerHTML).toBe("");
  finalRequest.resolve({ data: completedPayload() });
  await finalRequest.promise;
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(finalMount.container.innerHTML).toBe("");
});
it("isolates progress cache entries by authenticated user", async () => {
  mocks.flag = true;
  mocks.org = "org-test";
  mocks.user = "user-a";
  mocks.get.mockResolvedValue({ data: validPayload() });
  const { client, rerender } = renderProgress();
  await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(1));
  expect(
    client
      .getQueryCache()
      .findAll()
      .map(({ queryKey }) => queryKey),
  ).toContainEqual(["onboarding-progress", "user-a", "org-test"]);

  mocks.user = "user-b";
  rerender();
  await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
  expect(
    client
      .getQueryCache()
      .findAll()
      .map(({ queryKey }) => queryKey),
  ).toContainEqual(["onboarding-progress", "user-b", "org-test"]);
});
it("keeps forced out-of-order user responses isolated", async () => {
  const userAPayload = validPayload();
  const userBPayload = {
    ...validPayload(),
    completed_count: 1,
    next_action_key: "first_successful_run",
    items: [
      {
        key: "first_agent_created",
        completed_at: "2026-08-20T12:00:00Z",
      },
      { key: "first_successful_run", completed_at: null },
    ],
  };
  let resolveUserA: (response: { data: unknown }) => void = () => {
    throw new Error("User A request did not initialize");
  };
  let resolveUserB: (response: { data: unknown }) => void = () => {
    throw new Error("User B request did not initialize");
  };
  const userARequest = new Promise<{ data: unknown }>((resolve) => {
    resolveUserA = resolve;
  });
  const userBRequest = new Promise<{ data: unknown }>((resolve) => {
    resolveUserB = resolve;
  });
  mocks.get.mockReturnValueOnce(userARequest).mockReturnValueOnce(userBRequest);
  mocks.user = "user-a";
  const { client, result, rerender } = renderProgress();
  const userAKey = ["onboarding-progress", "user-a", "org-test"] as const;
  const userBKey = ["onboarding-progress", "user-b", "org-test"] as const;

  await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(1));
  const userAObserver = new QueryObserver(client, {
    queryKey: userAKey,
    enabled: false,
  });
  const unsubscribeUserA = userAObserver.subscribe(vi.fn());

  mocks.user = "user-b";
  rerender();
  await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
  const pendingKeys = client
    .getQueryCache()
    .findAll()
    .map(({ queryKey }) => queryKey);
  expect(pendingKeys).toContainEqual(userAKey);
  expect(pendingKeys).toContainEqual(userBKey);
  expect(client.getQueryData(userAKey)).toBeUndefined();
  expect(client.getQueryData(userBKey)).toBeUndefined();

  await act(async () => {
    resolveUserB({ data: userBPayload });
    await userBRequest;
  });
  await waitFor(() => expect(result.current.progress?.completed_count).toBe(1));
  expect(result.current.progress).toEqual(userBPayload);
  expect(client.getQueryData(userBKey)).toEqual(userBPayload);
  expect(client.getQueryData(userAKey)).toBeUndefined();

  await act(async () => {
    resolveUserA({ data: userAPayload });
    await userARequest;
  });
  await waitFor(() =>
    expect(client.getQueryData(userAKey)).toEqual(userAPayload),
  );
  expect(result.current.progress).toEqual(userBPayload);
  expect(client.getQueryData(userBKey)).toEqual(userBPayload);
  unsubscribeUserA();
});
const completedAt = "2026-08-20T12:00:00Z";
it.each([
  ["count mismatch", 1, "active", "first_agent_created", null, null],
  ["run without agent", 1, "active", "first_successful_run", null, completedAt],
  ["active with two/null next", 2, "active", null, completedAt, completedAt],
  ["dismissed with next", 0, "dismissed", "first_agent_created", null, null],
  ["completed with one", 1, "completed", null, completedAt, null],
])("rejects contradictory %s progress", async (_name, ...row) => {
  mocks.get.mockResolvedValue({
    data: {
      ...validPayload(),
      completed_count: row[0],
      state: row[1],
      next_action_key: row[2],
      items: [
        { key: "first_agent_created", completed_at: row[3] },
        { key: "first_successful_run", completed_at: row[4] },
      ],
    },
  });
  const { client } = renderProgress();
  await waitFor(() =>
    expect(client.getQueryCache().findAll()[0]?.state.data).toBeNull(),
  );
});
