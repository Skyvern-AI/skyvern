import { useCallback, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@clerk/clerk-react";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { OnboardingContext } from "./useOnboardingState";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";
import type { OnboardingStatePatch, OnboardingStateResponse } from "./types";

const QUERY_KEY = ["userOnboarding"] as const;

const PRODUCT_LAUNCH_DATE = "2024-10-01T00:00:00Z";

type Props = {
  children: React.ReactNode;
};

function OnboardingProvider({ children }: Readonly<Props>) {
  const credentialGetter = useCredentialGetter();
  const { isSignedIn } = useAuth();
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery<OnboardingStateResponse>({
    queryKey: QUERY_KEY,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get<OnboardingStateResponse>(
        "/users/me/onboarding",
      );
      return response.data;
    },
    enabled: !!credentialGetter && isSignedIn === true,
  });

  const mutation = useMutation<
    OnboardingStateResponse,
    unknown,
    OnboardingStatePatch
  >({
    mutationFn: async (patch) => {
      const client = await getClient(credentialGetter);
      const response = await client.post<OnboardingStateResponse>(
        "/users/me/onboarding",
        patch,
      );
      return response.data;
    },
    onMutate: async (patch) => {
      await queryClient.cancelQueries({ queryKey: QUERY_KEY });
      const previous =
        queryClient.getQueryData<OnboardingStateResponse>(QUERY_KEY);
      if (previous) {
        queryClient.setQueryData<OnboardingStateResponse>(QUERY_KEY, {
          ...previous,
          onboarding_state: { ...previous.onboarding_state, ...patch },
        });
      }
    },
    onError: () => {
      // Best-effort persistence: keep the optimistic state so a failed write
      // (offline / 5xx / 405) can never resurrect a dismissed modal or tour.
      OnboardingTelemetry.error("dashboard");
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });

  const isNewUser =
    data?.launch_date_at_signup != null &&
    new Date(data.launch_date_at_signup) >= new Date(PRODUCT_LAUNCH_DATE);

  const abVariant = data?.onboarding_state.ab_variant ?? null;

  useEffect(() => {
    if (abVariant) {
      OnboardingTelemetry.registerVariant(abVariant);
    }
  }, [abVariant]);

  const prevSaveAt = useRef<string | null | undefined>(undefined);
  const prevRunAt = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const s = data?.onboarding_state;
    if (!s) return;
    // emit the activation-funnel milestones once, on the observed null -> set transition
    if (prevSaveAt.current === null && s.first_save_at !== null) {
      OnboardingTelemetry.firstWorkflowCreated("dashboard");
    }
    if (prevRunAt.current === null && s.first_run_at !== null) {
      OnboardingTelemetry.firstRunCompleted("dashboard");
    }
    prevSaveAt.current = s.first_save_at;
    prevRunAt.current = s.first_run_at;
  }, [data?.onboarding_state]);

  const updateState = useCallback(
    (patch: OnboardingStatePatch) => {
      mutation.mutate(patch);
    },
    [mutation],
  );

  return (
    <OnboardingContext.Provider
      value={{
        state: data?.onboarding_state ?? null,
        isLoading,
        updateState,
        isNewUser,
        abVariant,
      }}
    >
      {children}
    </OnboardingContext.Provider>
  );
}

export { OnboardingProvider };
