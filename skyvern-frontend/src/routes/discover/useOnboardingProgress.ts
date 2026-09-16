import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useUser } from "@/hooks/useUser";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";
import {
  ONBOARDING_PROGRESS_FLAG,
  ONBOARDING_TRACK_FLAG,
} from "@/util/featureFlags";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
type ProgressActionKey = "first_agent_created" | "first_successful_run";
type OnboardingProgressItemV1 = {
  key: ProgressActionKey;
  completed_at: string | null;
};
type OnboardingProgressV1 = {
  version: "onboarding_progress_v1";
  state: "ineligible" | "active" | "dismissed" | "completed";
  completed_count: number;
  total_count: 2;
  next_action_key: ProgressActionKey | null;
  items: [OnboardingProgressItemV1, OnboardingProgressItemV1];
};
const ISO_TIMESTAMP =
  /^((?!0000)\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/;
function isTimestampOrNull(value: unknown): value is string | null {
  return (
    value === null ||
    (typeof value === "string" &&
      ISO_TIMESTAMP.test(value) &&
      new Date(`${value.slice(0, 10)}T00:00:00Z`).getUTCDate() ===
        Number(value.slice(8, 10)))
  );
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function parseItem(
  value: unknown,
  expectedKey: ProgressActionKey,
): OnboardingProgressItemV1 | null {
  if (
    !isRecord(value) ||
    Object.keys(value).length !== 2 ||
    value.key !== expectedKey ||
    !isTimestampOrNull(value.completed_at)
  )
    return null;
  return { key: expectedKey, completed_at: value.completed_at };
}
function parseOnboardingProgress(value: unknown): OnboardingProgressV1 | null {
  if (!isRecord(value) || Object.keys(value).length !== 6) return null;
  const {
    version,
    state,
    completed_count,
    total_count,
    next_action_key,
    items,
  } = value;
  if (
    version !== "onboarding_progress_v1" ||
    (state !== "ineligible" &&
      state !== "active" &&
      state !== "dismissed" &&
      state !== "completed") ||
    typeof completed_count !== "number" ||
    !Number.isInteger(completed_count) ||
    completed_count < 0 ||
    completed_count > 2 ||
    total_count !== 2 ||
    (next_action_key !== null &&
      next_action_key !== "first_agent_created" &&
      next_action_key !== "first_successful_run") ||
    !Array.isArray(items) ||
    items.length !== 2
  )
    return null;
  const firstAgent = parseItem(items[0], "first_agent_created");
  const firstRun = parseItem(items[1], "first_successful_run");
  if (!firstAgent || !firstRun) return null;
  const agentCompleted = firstAgent.completed_at !== null;
  const runCompleted = firstRun.completed_at !== null;
  const derivedCompletedCount = Number(agentCompleted) + Number(runCompleted);
  const expectedActiveAction = agentCompleted
    ? "first_successful_run"
    : "first_agent_created";
  if (
    completed_count !== derivedCompletedCount ||
    (runCompleted && !agentCompleted) ||
    (state === "active" &&
      (derivedCompletedCount === 2 ||
        next_action_key !== expectedActiveAction)) ||
    (state === "dismissed" &&
      (derivedCompletedCount === 2 || next_action_key !== null)) ||
    (state === "completed" &&
      (derivedCompletedCount !== 2 || next_action_key !== null)) ||
    (state === "ineligible" && next_action_key !== null)
  )
    return null;
  return {
    version,
    state,
    completed_count,
    total_count,
    next_action_key,
    items: [firstAgent, firstRun],
  };
}
function useOnboardingProgress() {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeUserId = useUser().get()?.id;
  const queryClient = useQueryClient();
  const trackFlag = useFeatureFlag(ONBOARDING_TRACK_FLAG);
  const progressFlag = useFeatureFlag(ONBOARDING_PROGRESS_FLAG);
  const flagOn = trackFlag === true || progressFlag === true;
  const flagOff = trackFlag === false && progressFlag === false;
  const enabled =
    flagOn && activeOrgId !== undefined && activeUserId !== undefined;
  const queryKey = getOrgScopedQueryKey(
    ["onboarding-progress", activeUserId],
    getActiveOrgQueryKeyScope(activeOrgId),
  );
  const { data, isError, refetch } = useQuery<OnboardingProgressV1>({
    queryKey,
    queryFn: async ({ signal }) => {
      const client = await getClient(credentialGetter);
      const response = await client.get<unknown>(
        "/users/me/onboarding/progress",
        { signal },
      );
      const progress = parseOnboardingProgress(response.data);
      if (progress === null)
        throw new Error("invalid onboarding progress payload");
      return progress;
    },
    enabled,
    retry: false,
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
  });
  const { isPending, mutate } = useMutation({
    mutationFn: async (action: "dismiss" | "restore") => {
      const client = await getClient(credentialGetter);
      await client.post(`/users/me/onboarding/progress/${action}`, {
        mutation_id: crypto.randomUUID(),
      });
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey, exact: true }),
  });
  const status: "disabled" | "loading" | "error" | "ready" = !enabled
    ? flagOff
      ? "disabled"
      : "loading"
    : data !== undefined
      ? "ready"
      : isError
        ? "error"
        : "loading";
  return {
    progress: data ?? null,
    status,
    isPending,
    refetch,
    dismiss: () => mutate("dismiss"),
    restore: () => mutate("restore"),
  };
}
export { isTimestampOrNull, useOnboardingProgress };
