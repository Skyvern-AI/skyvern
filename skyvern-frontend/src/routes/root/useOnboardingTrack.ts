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
const SECOND_AGENT_KEY = "second_agent_run" as const;
type TrackKey = (typeof TRACK_KEYS)[number] | typeof SECOND_AGENT_KEY;
const SELF_ATTESTED_KEYS: ReadonlySet<TrackKey> = new Set([
  "github_starred",
  "discord_joined",
  "social_followed",
]);
// Rows that count toward N/M. The teammate row stays out until its
// destination exists; community rows are never counted.
const COUNTED_KEYS: readonly TrackKey[] = [
  SECOND_AGENT_KEY,
  "first_scheduled_run",
  "first_api_run",
  "mcp_installed",
  "credential_saved",
];
type TrackState = "ineligible" | "active" | "dismissed" | "completed";
type OnboardingTrackItemV1 = {
  key: TrackKey;
  completed_at: string | null;
  verification: "server" | "self";
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
};

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
  const parsed: OnboardingTrackItemV1[] = [];
  for (const [index, item] of items.entries()) {
    const key =
      index < TRACK_KEYS.length
        ? TRACK_KEYS[index]
        : index === TRACK_KEYS.length
          ? SECOND_AGENT_KEY
          : undefined;
    if (key === undefined || !isRecord(item) || item.key !== key) return null;
    const completedAt = item.completed_at;
    if (!isTimestampOrNull(completedAt)) return null;
    const verification = SELF_ATTESTED_KEYS.has(key) ? "self" : "server";
    if (item.verification !== verification) return null;
    parsed.push({ key, completed_at: completedAt, verification });
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
  };
}

function useOnboardingTrack() {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeUserId = useUser().get()?.id;
  const queryClient = useQueryClient();
  const flag = useFeatureFlag(ONBOARDING_TRACK_FLAG);
  const enabled =
    flag === true && activeOrgId !== undefined && activeUserId !== undefined;
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
  // `false` flag disables the track.
  const status: "disabled" | "loading" | "error" | "ready" = !enabled
    ? flag === false
      ? "disabled"
      : "loading"
    : isError
      ? "error"
      : isLoading
        ? "loading"
        : "ready";
  return {
    track: track?.arm === "treatment" ? track : null,
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
  SECOND_AGENT_KEY,
  SELF_ATTESTED_KEYS,
  TRACK_KEYS,
  useOnboardingTrack,
};
export type { OnboardingTrackItemV1, OnboardingTrackV1, TrackKey };
