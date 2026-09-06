import type { ReactNode } from "react";
import {
  QueryClient,
  QueryClientProvider,
  QueryObserver,
} from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted<{
  get: ReturnType<typeof vi.fn>;
  flags: Record<string, boolean | undefined>;
  useFeatureFlag: ReturnType<typeof vi.fn<(name: string) => void>>;
  org: string | undefined;
  user: string | undefined;
}>(() => ({
  get: vi.fn(),
  flags: {},
  useFeatureFlag: vi.fn<(name: string) => void>(),
  org: "org-test",
  user: "user-test",
}));
vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ get: mocks.get }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));
vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: (name: string) => {
    mocks.useFeatureFlag(name);
    return mocks.flags[name];
  },
}));
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
import {
  ONBOARDING_PROGRESS_FLAG,
  ONBOARDING_TRACK_FLAG,
} from "@/util/featureFlags";
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
beforeEach(() => {
  vi.clearAllMocks();
  mocks.flags = {
    [ONBOARDING_TRACK_FLAG]: true,
    [ONBOARDING_PROGRESS_FLAG]: true,
  };
  mocks.org = "org-test";
  mocks.user = "user-test";
});
it.each([
  [undefined, "org-test", "user-test"],
  [true, undefined, "user-test"],
  [true, "org-test", undefined],
])("does not GET when onboarding progress is gated", (flag, org, user) => {
  mocks.flags = {
    [ONBOARDING_TRACK_FLAG]: flag,
    [ONBOARDING_PROGRESS_FLAG]: flag,
  };
  mocks.org = org;
  mocks.user = user;
  renderProgress();
  expect(mocks.get).not.toHaveBeenCalled();
});
it.each([
  ["track-only", true, false],
  ["progress-only", false, true],
  ["both", true, true],
  ["neither", false, false],
])("gates progress under %s flags", async (_, trackFlag, progressFlag) => {
  mocks.flags = {
    [ONBOARDING_TRACK_FLAG]: trackFlag,
    [ONBOARDING_PROGRESS_FLAG]: progressFlag,
  };
  mocks.get.mockResolvedValue({ data: validPayload() });

  const { result } = renderProgress();

  if (trackFlag || progressFlag) {
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(mocks.get).toHaveBeenCalledOnce();
    expect(result.current.progress).toEqual(validPayload());
  } else {
    expect(mocks.get).not.toHaveBeenCalled();
    expect(result.current.status).toBe("disabled");
  }
  expect(mocks.useFeatureFlag).toHaveBeenCalledWith(ONBOARDING_TRACK_FLAG);
  expect(mocks.useFeatureFlag).toHaveBeenCalledWith(ONBOARDING_PROGRESS_FLAG);
});
it("reports loading with null progress until the GET resolves, then ready", async () => {
  const pending = deferred<{ data: unknown }>();
  mocks.get.mockReturnValue(pending.promise);

  const { result } = renderProgress();

  await waitFor(() => expect(mocks.get).toHaveBeenCalledOnce());
  expect(result.current.status).toBe("loading");
  expect(result.current.progress).toBeNull();

  pending.resolve({ data: validPayload() });

  await waitFor(() => expect(result.current.status).toBe("ready"));
  expect(result.current.progress?.state).toBe("active");
});
it("reports an error with null progress after a 404", async () => {
  mocks.get.mockRejectedValueOnce({ response: { status: 404 } });

  const { result } = renderProgress();

  await waitFor(() => expect(result.current.status).toBe("error"));
  expect(result.current.progress).toBeNull();
});
it("reports an error with null progress when the first payload is malformed", async () => {
  mocks.get.mockResolvedValueOnce({ data: { version: "nope" } });

  const { result } = renderProgress();

  await waitFor(() => expect(result.current.status).toBe("error"));
  expect(result.current.progress).toBeNull();
});
it("keeps the last progress when a refetch fails", async () => {
  mocks.get
    .mockResolvedValueOnce({ data: validPayload() })
    .mockRejectedValueOnce({ response: { status: 500 } });

  const { result } = renderProgress();
  await waitFor(() => expect(result.current.status).toBe("ready"));

  await act(async () => {
    await result.current.refetch();
  });

  expect(mocks.get).toHaveBeenCalledTimes(2);
  expect(result.current.status).toBe("ready");
  expect(result.current.progress).toEqual(validPayload());
});
it("keeps the last progress when a refetch returns a malformed payload", async () => {
  mocks.get
    .mockResolvedValueOnce({ data: validPayload() })
    .mockResolvedValueOnce({ data: { version: "nope" } });

  const { result } = renderProgress();
  await waitFor(() => expect(result.current.status).toBe("ready"));

  await act(async () => {
    const refetched = await result.current.refetch();
    expect(refetched.isError).toBe(true);
    expect(refetched.data).toEqual(validPayload());
  });

  expect(mocks.get).toHaveBeenCalledTimes(2);
  expect(result.current.status).toBe("ready");
  expect(result.current.progress).toEqual(validPayload());
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
it("isolates progress cache entries by authenticated user", async () => {
  mocks.flags = {
    [ONBOARDING_TRACK_FLAG]: true,
    [ONBOARDING_PROGRESS_FLAG]: true,
  };
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
  const { result } = renderProgress();
  await waitFor(() => expect(result.current.status).toBe("error"));
  expect(result.current.progress).toBeNull();
});
