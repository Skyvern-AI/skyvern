import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { getClient } from "@/api/AxiosClient";
import type {
  DisablePhoneNumberRequest,
  EnablePhoneNumberRequest,
  OrganizationPhoneNumber,
  TwilioCredentialRequest,
  TwilioCredentialSafe,
  TwilioCredentialResponse,
  TwilioPhoneNumberInfo,
} from "@/api/types";
import { useToast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";
const TWILIO_QUERY_KEYS = [
  ["twilioCredential"],
  ["twilioPhoneNumbers"],
] as const;
export const REGISTERED_PHONE_NUMBERS_QUERY_KEY = [
  "registeredPhoneNumbers",
] as const;
type ApiErrorPayload = {
  detail?: unknown;
};
type TakeoverDetail = {
  current_sms_url: string | null;
  current_sms_method: string | null;
  current_sms_application_sid: string | null;
};
export class TakeoverRequiredError extends Error {
  readonly currentSmsUrl: string | null;
  readonly currentSmsMethod: string | null;
  readonly currentSmsApplicationSid: string | null;
  constructor(detail: TakeoverDetail) {
    super("This number already forwards to another webhook");
    this.name = "TakeoverRequiredError";
    this.currentSmsUrl = detail.current_sms_url;
    this.currentSmsMethod = detail.current_sms_method;
    this.currentSmsApplicationSid = detail.current_sms_application_sid;
  }
}
function getApiErrorMessage(error: unknown, fallback: string): string {
  if (!isAxiosError<ApiErrorPayload>(error)) {
    return error instanceof Error ? error.message : fallback;
  }
  const detail = error.response?.data?.detail;
  return typeof detail === "string" ? detail : error.message || fallback;
}
function isNotFoundError(error: unknown): boolean {
  return isAxiosError(error) && error.response?.status === 404;
}
function takeoverDetailFromError(error: unknown): TakeoverDetail | null {
  if (!isAxiosError<ApiErrorPayload>(error) || error.response?.status !== 409) {
    return null;
  }
  const detail = error.response.data?.detail;
  if (!detail || typeof detail !== "object") {
    return null;
  }
  return {
    current_sms_url:
      "current_sms_url" in detail && typeof detail.current_sms_url === "string"
        ? detail.current_sms_url
        : null,
    current_sms_method:
      "current_sms_method" in detail &&
      typeof detail.current_sms_method === "string"
        ? detail.current_sms_method
        : null,
    current_sms_application_sid:
      "current_sms_application_sid" in detail &&
      typeof detail.current_sms_application_sid === "string"
        ? detail.current_sms_application_sid
        : null,
  };
}
function useInvalidateTwilioQueries({
  includeRegisteredPhoneNumbers = false,
  includeCredential = true,
}: {
  includeRegisteredPhoneNumbers?: boolean;
  includeCredential?: boolean;
} = {}) {
  const queryClient = useQueryClient();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);
  const twilioQueryKeys = includeCredential
    ? TWILIO_QUERY_KEYS
    : TWILIO_QUERY_KEYS.slice(1);
  const scopedTwilioQueryKeys = twilioQueryKeys.map((queryKey) =>
    getOrgScopedQueryKey(queryKey, activeOrgQueryKeyScope),
  );
  const queryKeys: ReadonlyArray<ReadonlyArray<unknown>> =
    includeRegisteredPhoneNumbers
      ? [
          ...scopedTwilioQueryKeys,
          getOrgScopedQueryKey(
            REGISTERED_PHONE_NUMBERS_QUERY_KEY,
            activeOrgQueryKeyScope,
          ),
        ]
      : scopedTwilioQueryKeys;
  return () =>
    Promise.all(
      queryKeys.map((queryKey) => queryClient.invalidateQueries({ queryKey })),
    );
}
function useMutationErrorToast() {
  const { toast } = useToast();
  return (error: unknown, fallback: string) => {
    toast({
      title: "Error",
      description: getApiErrorMessage(error, fallback),
      variant: "destructive",
    });
  };
}
export function useTwilioCredentialQuery() {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);
  return useQuery<{
    available: boolean;
    credential: TwilioCredentialSafe | null;
  }>({
    queryKey: getOrgScopedQueryKey(
      TWILIO_QUERY_KEYS[0],
      activeOrgQueryKeyScope,
    ),
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      try {
        const response = await client.get<TwilioCredentialResponse>(
          "/integrations/twilio/credentials",
        );
        return {
          available: true,
          credential: response.data.credential,
        };
      } catch (error) {
        if (isNotFoundError(error)) {
          return { available: false, credential: null };
        }
        throw error;
      }
    },
  });
}
export function useTwilioPhoneNumbersQuery({ enabled }: { enabled: boolean }) {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);
  return useQuery<TwilioPhoneNumberInfo[]>({
    queryKey: getOrgScopedQueryKey(
      TWILIO_QUERY_KEYS[1],
      activeOrgQueryKeyScope,
    ),
    enabled,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get<TwilioPhoneNumberInfo[]>(
        "/integrations/twilio/phone-numbers",
      );
      return response.data;
    },
    retry: (failureCount, error) => !isNotFoundError(error) && failureCount < 3,
  });
}
export function useRegisteredPhoneNumbersQuery() {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);
  return useQuery<OrganizationPhoneNumber[]>({
    queryKey: getOrgScopedQueryKey(
      REGISTERED_PHONE_NUMBERS_QUERY_KEY,
      activeOrgQueryKeyScope,
    ),
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      try {
        const response = await client.get<OrganizationPhoneNumber[]>(
          "/integrations/twilio/phone-number-registry",
        );
        return response.data;
      } catch (error) {
        if (isNotFoundError(error)) return [];
        throw error;
      }
    },
  });
}
export function useSaveTwilioCredentialMutation() {
  const credentialGetter = useCredentialGetter();
  const invalidateQueries = useInvalidateTwilioQueries();
  const showError = useMutationErrorToast();
  const { toast } = useToast();
  const mutation = useMutation({
    gcTime: 0,
    mutationFn: async (credential: TwilioCredentialRequest) => {
      const client = await getClient(credentialGetter);
      const response = await client.post<TwilioCredentialResponse>(
        "/integrations/twilio/credentials",
        { credential },
      );
      return response.data.credential;
    },
    onSuccess: () => {
      toast({
        title: "Twilio connected",
        description: "Your Twilio phone numbers are ready to configure.",
      });
    },
    onError: (error: unknown) => {
      showError(error, "Failed to connect Twilio");
    },
    onSettled: async () => {
      await invalidateQueries();
    },
  });
  const { isSuccess, isError, reset } = mutation;
  useEffect(() => {
    if (isSuccess || isError) {
      reset();
    }
  }, [isSuccess, isError, reset]);
  return mutation;
}
export function useDeleteTwilioCredentialMutation() {
  const credentialGetter = useCredentialGetter();
  const invalidateQueries = useInvalidateTwilioQueries();
  const showError = useMutationErrorToast();
  const { toast } = useToast();
  return useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.delete<{ success: boolean }>(
        "/integrations/twilio/credentials",
      );
      return response.data;
    },
    onSuccess: () => {
      toast({
        title: "Twilio disconnected",
        description: "Your Twilio credentials were removed.",
      });
    },
    onError: (error: unknown) => {
      showError(error, "Failed to disconnect Twilio");
    },
    onSettled: async () => {
      await invalidateQueries();
    },
  });
}
export function useEnablePhoneNumberMutation() {
  const credentialGetter = useCredentialGetter();
  const invalidateQueries = useInvalidateTwilioQueries({
    includeRegisteredPhoneNumbers: true,
    includeCredential: false,
  });
  const showError = useMutationErrorToast();
  const { toast } = useToast();
  return useMutation({
    mutationFn: async (request: EnablePhoneNumberRequest) => {
      const client = await getClient(credentialGetter);
      try {
        const response = await client.post<OrganizationPhoneNumber>(
          "/integrations/twilio/phone-numbers/enable",
          request,
        );
        return response.data;
      } catch (error) {
        const takeoverDetail = takeoverDetailFromError(error);
        if (takeoverDetail) {
          throw new TakeoverRequiredError(takeoverDetail);
        }
        throw error;
      }
    },
    onSuccess: () => {
      toast({
        title: "SMS forwarding enabled",
        description:
          "Skyvern can now receive verification codes on this number.",
      });
    },
    onError: (error: unknown) => {
      if (error instanceof TakeoverRequiredError) {
        return;
      }
      showError(error, "Failed to enable this phone number");
    },
    onSettled: async () => {
      await invalidateQueries();
    },
  });
}
export function useDisablePhoneNumberMutation() {
  const credentialGetter = useCredentialGetter();
  const invalidateQueries = useInvalidateTwilioQueries({
    includeRegisteredPhoneNumbers: true,
    includeCredential: false,
  });
  const showError = useMutationErrorToast();
  const { toast } = useToast();
  return useMutation({
    mutationFn: async (request: DisablePhoneNumberRequest) => {
      const client = await getClient(credentialGetter);
      const response = await client.post<OrganizationPhoneNumber>(
        "/integrations/twilio/phone-numbers/disable",
        request,
      );
      return response.data;
    },
    onSuccess: () => {
      toast({
        title: "SMS forwarding disabled",
        description: "Twilio's previous webhook settings were restored.",
      });
    },
    onError: (error: unknown) => {
      showError(error, "Failed to disable this phone number");
    },
    onSettled: async () => {
      await invalidateQueries();
    },
  });
}
