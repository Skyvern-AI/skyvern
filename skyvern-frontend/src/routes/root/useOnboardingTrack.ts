import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useUser } from "@/hooks/useUser";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";
import { isTimestampOrNull } from "@/routes/discover/useOnboardingProgress";
import { ONBOARDING_TRACK_FLAG } from "@/util/featureFlags";
import { isRecord } from "@/util/utils";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

const TRACK_KEYS = [
  "first_scheduled_run",
  "first_api_run",
  "mcp_installed",
  "teammate_invited",
  "credential_saved",
  "github_starred",
  "discord_joined",
  "social_followed",
] as const;
// The layout served once activation rewards v2 is live; the parser accepts either
// so a backend deploy ahead of the frontend doesn't blank the track.
const REWARD_TRACK_KEYS = [
  "questionnaire_completed",
  "first_successful_run",
  "run_feedback_given",
  "first_scheduled_run",
  "credential_saved",
  "agent_edited",
  "github_starred",
  "discord_joined",
] as const;
const SECOND_AGENT_KEY = "second_agent_run" as const;
type TrackKey =
  | (typeof TRACK_KEYS)[number]
  | (typeof REWARD_TRACK_KEYS)[number]
  | typeof SECOND_AGENT_KEY;
const SELF_ATTESTED_KEYS: ReadonlySet<TrackKey> = new Set([
  "github_starred",
  "discord_joined",
  "social_followed",
]);
type TrackRewardState =
  | "unearned"
  | "pending"
  | "blocked"
  | "granted"
  | "reversed";
// Rows that count toward N/M. The teammate row stays out until its
// destination exists; community rows are never counted.
const COUNTED_KEYS: readonly TrackKey[] = [
  SECOND_AGENT_KEY,
  "first_scheduled_run",
  "first_api_run",
  "mcp_installed",
  "agent_edited",
  "run_feedback_given",
  "credential_saved",
];
type TrackState = "ineligible" | "active" | "dismissed" | "completed";
type OnboardingTrackItemV1 = {
  key: TrackKey;
  completed_at: string | null;
  verification: "server" | "self";
  // Credits the milestone grants; null while reward grants are disabled.
  reward_credits?: number | null;
  reward_state?: TrackRewardState | null;
};
type TrackMutation =
  | { action: "dismiss" | "restore" }
  | { action: "attest"; key: TrackKey };
type OnboardingTrackV1 = {
  version: "onboarding_track_v1";
  state: TrackState;
  arm: "control" | "treatment";
  completed_count: number;
  total_count: 8 | 9;
  items: OnboardingTrackItemV1[];
  // Credits for answering the onboarding questionnaire; null while reward grants are disabled.
  questionnaire_reward_credits?: number | null;
  reward_credits_earned?: number | null;
  reward_credits_cap?: number | null;
};

function isTrackRewardState(value: unknown): value is TrackRewardState {
  return (
    value === "unearned" ||
    value === "pending" ||
    value === "blocked" ||
    value === "granted" ||
    value === "reversed"
  );
}

function wholeCredits(value: unknown, allowZero = false): number | null {
  return typeof value === "number" &&
    Number.isInteger(value) &&
    (allowZero ? value >= 0 : value > 0)
    ? value
    : null;
}

function isTrackState(value: unknown): value is TrackState {
  return (
    value === "ineligible" ||
    value === "active" ||
    value === "dismissed" ||
    value === "completed"
  );
}

function parseOnboardingTrack(value: unknown): OnboardingTrackV1 | null {
  if (!isRecord(value) || value.version !== "onboarding_track_v1") return null;
  const { state, arm, completed_count, total_count, items } = value;
  if (!isTrackState(state)) return null;
  if (arm !== "control" && arm !== "treatment") return null;
  if (
    !Array.isArray(items) ||
    (items.length !== 8 && items.length !== 9) ||
    total_count !== items.length
  )
    return null;
  const first: unknown = items[0];
  const layout: readonly TrackKey[] =
    isRecord(first) && first.key === REWARD_TRACK_KEYS[0]
      ? REWARD_TRACK_KEYS
      : TRACK_KEYS;
  const parsed: OnboardingTrackItemV1[] = [];
  for (const [index, item] of items.entries()) {
    const key =
      index < layout.length
        ? layout[index]
        : index === layout.length
          ? SECOND_AGENT_KEY
          : undefined;
    if (key === undefined || !isRecord(item) || item.key !== key) return null;
    const completedAt = item.completed_at;
    if (!isTimestampOrNull(completedAt)) return null;
    const verification = SELF_ATTESTED_KEYS.has(key) ? "self" : "server";
    if (item.verification !== verification) return null;
    parsed.push({
      key,
      completed_at: completedAt,
      verification,
      reward_credits: wholeCredits(item.reward_credits),
      reward_state: isTrackRewardState(item.reward_state)
        ? item.reward_state
        : null,
    });
  }
  const derivedCount = parsed.filter((row) => row.completed_at !== null).length;
  if (completed_count !== derivedCount) return null;
  if ((state === "completed") !== (derivedCount === items.length)) return null;
  return {
    version: "onboarding_track_v1",
    state,
    arm,
    completed_count: derivedCount,
    total_count: items.length,
    items: parsed,
    questionnaire_reward_credits: wholeCredits(
      value.questionnaire_reward_credits,
    ),
    reward_credits_earned: wholeCredits(value.reward_credits_earned, true),
    reward_credits_cap: wholeCredits(value.reward_credits_cap),
  };
}

type UseOnboardingTrackOptions = {
  /**
   * For surfaces outside the track experiment: also load for users without a
   * Clerk organization, and return the track for both arms.
   */
  outsideExperiment?: boolean;
  /** When false, never fetch; the track reads as null. */
  enabled?: boolean;
};

function useOnboardingTrack({
  outsideExperiment = false,
  enabled: callerEnabled = true,
}: UseOnboardingTrackOptions = {}) {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeUserId = useUser().get()?.id;
  const queryClient = useQueryClient();
  const trackFlag = useFeatureFlag(ONBOARDING_TRACK_FLAG);
  // Surfaces outside the experiment gate themselves, so only the holdout's own UI needs the flag.
  const enabled =
    callerEnabled &&
    (trackFlag === true || outsideExperiment) &&
    (activeOrgId !== undefined || outsideExperiment) &&
    activeUserId !== undefined;
  const queryKey = getOrgScopedQueryKey(
    ["onboarding-track", activeUserId],
    getActiveOrgQueryKeyScope(activeOrgId),
  );
  const {
    data,
    isError,
    isPending: isLoading,
    refetch,
  } = useQuery<OnboardingTrackV1 | null>({
    queryKey,
    queryFn: async ({ signal }) => {
      const client = await getClient(credentialGetter);
      const response = await client.get<unknown>("/users/me/onboarding/track", {
        signal,
      });
      return parseOnboardingTrack(response.data);
    },
    enabled,
    retry: false,
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
  });
  const { isPending, mutate } = useMutation({
    mutationFn: async (input: TrackMutation) => {
      const client = await getClient(credentialGetter);
      await client.post(`/users/me/onboarding/track/${input.action}`, {
        ...(input.action === "attest" ? { key: input.key } : {}),
        mutation_id: crypto.randomUUID(),
      });
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey, exact: true }),
  });
  const track = enabled && !isError ? (data ?? null) : null;
  // An undefined flag, org, or user is still resolving; only an explicit
  // `false` flag or `enabled: false` disables the track.
  const status: "disabled" | "loading" | "error" | "ready" = !enabled
    ? !callerEnabled || (trackFlag === false && !outsideExperiment)
      ? "disabled"
      : "loading"
    : isError
      ? "error"
      : isLoading
        ? "loading"
        : "ready";
  return {
    // The track experiment's own UI shows only where its flag is on, not for journey-only orgs.
    track:
      (trackFlag === true && track?.arm === "treatment") || outsideExperiment
        ? track
        : null,
    status,
    isPending,
    refetch,
    dismiss: () => mutate({ action: "dismiss" }),
    restore: () => mutate({ action: "restore" }),
    attest: (key: TrackKey) => mutate({ action: "attest", key }),
  };
}

function countedTrackItems(
  track: OnboardingTrackV1,
  credentialUnlocked: boolean,
): OnboardingTrackItemV1[] {
  return track.items.filter(
    (item) =>
      COUNTED_KEYS.includes(item.key) &&
      (credentialUnlocked || item.key !== "credential_saved"),
  );
}

export {
  countedTrackItems,
  parseOnboardingTrack,
  REWARD_TRACK_KEYS,
  SECOND_AGENT_KEY,
  SELF_ATTESTED_KEYS,
  TRACK_KEYS,
  useOnboardingTrack,
};
export type {
  OnboardingTrackItemV1,
  OnboardingTrackV1,
  TrackKey,
  TrackRewardState,
};
