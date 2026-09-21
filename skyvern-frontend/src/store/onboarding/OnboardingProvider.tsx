import { useCallback, useEffect, useMemo, useRef } from "react";
import { isAxiosError } from "axios";
import {
  CancelledError,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useAuth } from "@clerk/clerk-react";
import { getClientWithRequestHeaders } from "@/api/AxiosClient";
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
  generation: number;
  queryKey: OnboardingQueryKey;
};

type OnboardingQueryKey = readonly [
  "userOnboarding",
  string | null | undefined,
  string | null,
];
type ScopedWrite<Patch> = {
  patch: Patch;
  generation: number;
  queryKey: OnboardingQueryKey;
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
  const { isSignedIn, userId, orgId } = useAuth();
  const queryClient = useQueryClient();
  const queryKey = useMemo(
    (): OnboardingQueryKey => ["userOnboarding", userId, orgId ?? null],
    [userId, orgId],
  );
  const legacyWriteVersionRef = useRef(0);
  const legacyWritesRef = useRef<LegacyWrite[]>([]);
  const currentScopeRef = useRef({ queryKey, generation: 0 });
  const conflictRefetchPendingRef = useRef(false);
  const channelRef = useRef<BroadcastChannel | null>(null);
  const activeRef = useRef(true);
  if (currentScopeRef.current.queryKey !== queryKey) {
    legacyWritesRef.current = [];
    legacyWriteVersionRef.current = 0;
    conflictRefetchPendingRef.current = false;
    currentScopeRef.current = {
      queryKey,
      generation: currentScopeRef.current.generation + 1,
    };
  }
  const generation = currentScopeRef.current.generation;
  const isCurrent = useCallback(
    (value: number) =>
      activeRef.current && currentScopeRef.current.generation === value,
    [],
  );
  useEffect(() => {
    activeRef.current = true;
    return () => {
      activeRef.current = false;
    };
  }, []);
  useEffect(
    () => () => {
      queryClient.removeQueries({ queryKey, exact: true });
    },
    [queryClient, queryKey],
  );
  const requestClient = useCallback(
    async (writeGeneration: number) => {
      if (!isCurrent(writeGeneration)) throw new CancelledError();
      const request = await getClientWithRequestHeaders(credentialGetter);
      if (!isCurrent(writeGeneration)) throw new CancelledError();
      // A stale global API key must not override the bearer's organization.
      request.headers.set("X-API-Key", null);
      return request;
    },
    [credentialGetter, isCurrent],
  );
  const { data, isLoading } = useQuery<OnboardingStateResponse>({
    queryKey,
    queryFn: async () => {
      const { client, headers } = await requestClient(generation);
      const response = await client.get<OnboardingStateResponse>(
        "/users/me/onboarding",
        { headers },
      );
      if (!isCurrent(generation)) throw new CancelledError();
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
    async ({
      patch,
      generation: writeGeneration,
    }: ScopedWrite<ConfirmedPatch>) => {
      const { client, headers } = await requestClient(writeGeneration);
      const response = await client.post<OnboardingStateResponse>(
        "/users/me/onboarding",
        patch,
        { headers },
      );
      if (!isCurrent(writeGeneration)) throw new CancelledError();
      return response.data;
    },
    [requestClient, isCurrent],
  );

  const legacyMutation = useMutation<
    OnboardingStateResponse,
    unknown,
    ScopedWrite<LegacyOnboardingStatePatch>,
    LegacyMutationContext
  >({
    scope: MUTATION_SCOPE,
    mutationFn: writeState,
    onMutate: async ({
      patch,
      generation: writeGeneration,
      queryKey: writeKey,
    }) => {
      if (!isCurrent(writeGeneration)) throw new CancelledError();
      const legacyWriteVersion = legacyWriteVersionRef.current + 1;
      legacyWriteVersionRef.current = legacyWriteVersion;
      legacyWritesRef.current.push({
        version: legacyWriteVersion,
        patch,
        status: "pending",
      });
      if (!conflictRefetchPendingRef.current) {
        await queryClient.cancelQueries({ queryKey: writeKey });
      }
      if (!isCurrent(writeGeneration)) throw new CancelledError();
      const previous =
        queryClient.getQueryData<OnboardingStateResponse>(writeKey);
      if (previous) {
        queryClient.setQueryData<OnboardingStateResponse>(writeKey, {
          ...previous,
          onboarding_state: { ...previous.onboarding_state, ...patch },
        });
      }
      return {
        legacyWriteVersion,
        generation: writeGeneration,
        queryKey: writeKey,
      };
    },
    onError: (error, write, context) => {
      if (!isCurrent(write.generation) || error instanceof CancelledError)
        return;
      if (context && isCurrent(context.generation)) {
        const write = legacyWritesRef.current.find(
          ({ version }) => version === context.legacyWriteVersion,
        );
        if (write) write.status = "failed";
      }
      OnboardingTelemetry.error("dashboard");
    },
    onSuccess: async (_nextState, _patch, context) => {
      if (!isCurrent(context.generation)) return;
      const write = legacyWritesRef.current.find(
        ({ version }) => version === context.legacyWriteVersion,
      );
      if (write) write.status = "succeeded";
      await queryClient.invalidateQueries({ queryKey: context.queryKey });
      if (!isCurrent(context.generation)) return;
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
    ScopedWrite<ConfirmedPatch>
  >({
    scope: MUTATION_SCOPE,
    mutationFn: async (write) => {
      try {
        return await writeState(write);
      } catch (error) {
        if (!isCurrent(write.generation)) throw new CancelledError();
        if (!isAxiosError(error) || error.response?.status !== 409) throw error;
        conflictRefetchPendingRef.current = true;
        await queryClient
          .invalidateQueries(
            { queryKey: write.queryKey },
            { throwOnError: true },
          )
          .finally(() => {
            if (isCurrent(write.generation)) {
              conflictRefetchPendingRef.current = false;
            }
          });
        throw error;
      }
    },
    onMutate: async (write) => {
      if (!isCurrent(write.generation)) throw new CancelledError();
      if (!conflictRefetchPendingRef.current) {
        await queryClient.cancelQueries({ queryKey: write.queryKey });
      }
    },
    onSuccess: (nextState, write) => {
      if (!isCurrent(write.generation)) return;
      if (isAuthoritativeConfirmedResponse(nextState)) {
        queryClient.setQueryData<OnboardingStateResponse>(
          write.queryKey,
          (current) => mergeConfirmedResponse(current, nextState),
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
      void queryClient.invalidateQueries({ queryKey: write.queryKey });
    },
    onError: (error, write) => {
      if (!isCurrent(write.generation) || error instanceof CancelledError)
        return;
      OnboardingTelemetry.error("dashboard");
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
      legacyMutation.mutate({ patch, generation, queryKey });
    },
    [legacyMutation, generation, queryKey],
  );
  const updateStateConfirmed = useCallback(
    async (patch: ConfirmedPatch): Promise<ConfirmedWriteResult> => {
      try {
        return await confirmedMutation.mutateAsync({
          patch,
          generation,
          queryKey,
        });
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
              case "project_owner_organization_conflict":
                return { code: detail };
              default:
                return { code: "unknown" };
            }
          }
          if (
            status === 422 &&
            patch.questionnaire &&
            "project_owner" in patch.questionnaire
          ) {
            return { code: "project_owner_invalid" };
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
    [confirmedMutation, generation, queryKey],
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
