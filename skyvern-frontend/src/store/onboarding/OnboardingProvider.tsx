import { useCallback, useEffect, useMemo, useRef } from "react";
import { isAxiosError } from "axios";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@clerk/clerk-react";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { OnboardingContext } from "./useOnboardingState";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";
import type {
  ConfirmedPatch,
  ConfirmedWriteResult,
  LegacyOnboardingStatePatch,
  OnboardingStateResponse,
} from "./types";

const PRODUCT_LAUNCH_DATE = "2024-10-01T00:00:00Z";
const MUTATION_SCOPE = { id: "userOnboarding" } as const;

const ONBOARDING_BROADCAST_CHANNEL = "skyvern-user-onboarding";

type QuestionnaireReservedMessage = {
  type: "questionnaire-reserved";
  userId: string;
  promptedAt: string;
};

function parseQuestionnaireReservedMessage(
  value: unknown,
): QuestionnaireReservedMessage | null {
  if (typeof value !== "object" || value === null) return null;
  const type = Reflect.get(value, "type");
  const userId = Reflect.get(value, "userId");
  const promptedAt = Reflect.get(value, "promptedAt");
  return type === "questionnaire-reserved" &&
    typeof userId === "string" &&
    typeof promptedAt === "string"
    ? { type, userId, promptedAt }
    : null;
}

function isAuthoritativeConfirmedResponse(
  response: OnboardingStateResponse,
): boolean {
  const status = response.questionnaire_prompt_result?.status;
  return status !== "flag_disabled" && status !== "ineligible";
}

function mergeConfirmedResponse(
  current: OnboardingStateResponse | undefined,
  response: OnboardingStateResponse,
): OnboardingStateResponse {
  return {
    ...response,
    recovery_guidance_assignment:
      response.recovery_guidance_assignment ??
      current?.recovery_guidance_assignment ??
      null,
  };
}

type LegacyWrite = {
  version: number;
  patch: LegacyOnboardingStatePatch;
  status: "pending" | "failed" | "succeeded";
};

type LegacyMutationContext = {
  legacyWriteVersion: number;
  userId: string | null | undefined;
  queryKey: readonly ["userOnboarding", string | null | undefined];
};

function legacyFieldsToReplay(
  writes: LegacyWrite[],
  version: number,
): LegacyOnboardingStatePatch {
  const fields: LegacyOnboardingStatePatch = {};
  for (const write of writes) {
    if (write.version >= version || write.status !== "succeeded") {
      Object.assign(fields, write.patch);
    } else {
      for (const field of Object.keys(write.patch)) {
        Reflect.deleteProperty(fields, field);
      }
    }
  }
  return fields;
}

function mergeNewerLegacyFields(
  authoritativeState: OnboardingStateResponse,
  legacyFields: LegacyOnboardingStatePatch,
): OnboardingStateResponse {
  return {
    ...authoritativeState,
    onboarding_state: {
      ...authoritativeState.onboarding_state,
      ...legacyFields,
    },
  };
}

type Props = {
  children: React.ReactNode;
};

function OnboardingProvider({ children }: Readonly<Props>) {
  const credentialGetter = useCredentialGetter();
  const { isSignedIn, userId } = useAuth();
  const queryClient = useQueryClient();
  const queryKey = useMemo(
    (): readonly ["userOnboarding", typeof userId] => [
      "userOnboarding",
      userId,
    ],
    [userId],
  );
  const legacyWriteVersionRef = useRef(0);
  const legacyWritesRef = useRef<LegacyWrite[]>([]);
  const previousUserIdRef = useRef<string | null | undefined>(undefined);
  const conflictRefetchPendingRef = useRef(false);
  const channelRef = useRef<BroadcastChannel | null>(null);

  if (previousUserIdRef.current !== userId) {
    legacyWritesRef.current = [];
    legacyWriteVersionRef.current = 0;
    previousUserIdRef.current = userId;
  }
  const { data, isLoading } = useQuery<OnboardingStateResponse>({
    queryKey,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get<OnboardingStateResponse>(
        "/users/me/onboarding",
      );
      if (previousUserIdRef.current !== userId) {
        return response.data;
      }
      const legacyFields = legacyFieldsToReplay(
        legacyWritesRef.current,
        legacyWriteVersionRef.current + 1,
      );
      return Object.keys(legacyFields).length === 0
        ? response.data
        : mergeNewerLegacyFields(response.data, legacyFields);
    },
    enabled: !!credentialGetter && isSignedIn === true && !!userId,
  });

  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const channel = new BroadcastChannel(ONBOARDING_BROADCAST_CHANNEL);
    channelRef.current = channel;
    const listener = (event: MessageEvent<unknown>) => {
      const message = parseQuestionnaireReservedMessage(event.data);
      if (!message || message.userId !== userId) return;
      queryClient.setQueryData<OnboardingStateResponse>(queryKey, (current) =>
        current
          ? {
              ...current,
              onboarding_state: {
                ...current.onboarding_state,
                questionnaire_prompted_at: message.promptedAt,
              },
            }
          : current,
      );
      void queryClient.invalidateQueries({ queryKey });
    };
    channel.addEventListener("message", listener);
    return () => {
      channel.removeEventListener("message", listener);
      channel.close();
      if (channelRef.current === channel) channelRef.current = null;
    };
  }, [queryClient, queryKey, userId]);

  const writeState = useCallback(
    async (patch: ConfirmedPatch) => {
      const client = await getClient(credentialGetter);
      const response = await client.post<OnboardingStateResponse>(
        "/users/me/onboarding",
        patch,
      );
      return response.data;
    },
    [credentialGetter],
  );

  const legacyMutation = useMutation<
    OnboardingStateResponse,
    unknown,
    LegacyOnboardingStatePatch,
    LegacyMutationContext
  >({
    scope: MUTATION_SCOPE,
    mutationFn: writeState,
    onMutate: async (patch) => {
      const legacyWriteVersion = legacyWriteVersionRef.current + 1;
      legacyWriteVersionRef.current = legacyWriteVersion;
      legacyWritesRef.current.push({
        version: legacyWriteVersion,
        patch,
        status: "pending",
      });
      if (!conflictRefetchPendingRef.current) {
        await queryClient.cancelQueries({ queryKey: queryKey });
      }
      const previous =
        queryClient.getQueryData<OnboardingStateResponse>(queryKey);
      if (previous) {
        queryClient.setQueryData<OnboardingStateResponse>(queryKey, {
          ...previous,
          onboarding_state: { ...previous.onboarding_state, ...patch },
        });
      }
      return { legacyWriteVersion, userId, queryKey };
    },
    onError: (_error, _patch, context) => {
      if (context && context.userId === previousUserIdRef.current) {
        const write = legacyWritesRef.current.find(
          ({ version }) => version === context.legacyWriteVersion,
        );
        if (write) write.status = "failed";
      }
      OnboardingTelemetry.error("dashboard");
    },
    onSuccess: async (_nextState, _patch, context) => {
      if (context.userId === previousUserIdRef.current) {
        const write = legacyWritesRef.current.find(
          ({ version }) => version === context.legacyWriteVersion,
        );
        if (write) write.status = "succeeded";
      }
      await queryClient.invalidateQueries({ queryKey: context.queryKey });
      if (context.userId !== previousUserIdRef.current) return;
      const newerFields = legacyFieldsToReplay(
        legacyWritesRef.current,
        context.legacyWriteVersion,
      );
      if (Object.keys(newerFields).length === 0) return;
      queryClient.setQueryData<OnboardingStateResponse>(
        context.queryKey,
        (currentState) =>
          currentState
            ? mergeNewerLegacyFields(currentState, newerFields)
            : currentState,
      );
    },
  });

  const confirmedMutation = useMutation<
    OnboardingStateResponse,
    unknown,
    ConfirmedPatch
  >({
    scope: MUTATION_SCOPE,
    mutationFn: async (patch) => {
      try {
        return await writeState(patch);
      } catch (error) {
        if (!isAxiosError(error) || error.response?.status !== 409) throw error;
        conflictRefetchPendingRef.current = true;
        await queryClient
          .invalidateQueries({ queryKey: queryKey }, { throwOnError: true })
          .finally(() => (conflictRefetchPendingRef.current = false));
        throw error;
      }
    },
    onMutate: async () => {
      if (!conflictRefetchPendingRef.current) {
        await queryClient.cancelQueries({ queryKey: queryKey });
      }
    },
    onSuccess: (nextState) => {
      if (isAuthoritativeConfirmedResponse(nextState)) {
        queryClient.setQueryData<OnboardingStateResponse>(queryKey, (current) =>
          mergeConfirmedResponse(current, nextState),
        );
        const promptedAt = nextState.onboarding_state.questionnaire_prompted_at;
        if (typeof userId === "string" && promptedAt) {
          try {
            channelRef.current?.postMessage({
              type: "questionnaire-reserved",
              userId,
              promptedAt,
            } satisfies QuestionnaireReservedMessage);
          } catch {
            // Cross-tab synchronization is best effort; the background refetch remains authoritative.
          }
        }
      }
      void queryClient.invalidateQueries({ queryKey });
    },
    onError: () => OnboardingTelemetry.error("dashboard"),
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
    const onboardingState = data?.onboarding_state;
    if (!onboardingState) return;
    if (prevSaveAt.current === null && onboardingState.first_save_at !== null) {
      OnboardingTelemetry.firstWorkflowCreated("dashboard");
    }
    if (prevRunAt.current === null && onboardingState.first_run_at !== null) {
      OnboardingTelemetry.firstRunCompleted("dashboard");
    }
    prevSaveAt.current = onboardingState.first_save_at;
    prevRunAt.current = onboardingState.first_run_at;
  }, [data?.onboarding_state]);

  const updateState = useCallback(
    (patch: LegacyOnboardingStatePatch) => {
      legacyMutation.mutate(patch);
    },
    [legacyMutation],
  );
  const updateStateConfirmed = useCallback(
    async (patch: ConfirmedPatch): Promise<ConfirmedWriteResult> => {
      try {
        return await confirmedMutation.mutateAsync(patch);
      } catch (error) {
        if (isAxiosError<{ detail?: string }>(error)) {
          const status = error.response?.status;
          const detail = error.response?.data?.detail;
          if (status === 409) {
            switch (detail) {
              case "questionnaire_revision_conflict":
              case "questionnaire_requires_user_intent":
              case "questionnaire_update_requires_response":
              case "questionnaire_invalid_transition":
                return { code: detail };
              default:
                return { code: "unknown" };
            }
          }
          if (
            status === 403 &&
            detail === "onboarding_questionnaire_disabled"
          ) {
            return { code: detail };
          }
        }
        throw error;
      }
    },
    [confirmedMutation],
  );

  return (
    <OnboardingContext.Provider
      value={{
        state: data?.onboarding_state ?? null,
        isLoading,
        updateState,
        updateStateConfirmed,
        isNewUser,
        abVariant,
        recoveryGuidanceAssignment: data?.recovery_guidance_assignment ?? null,
      }}
    >
      {children}
    </OnboardingContext.Provider>
  );
}

export { OnboardingProvider };
