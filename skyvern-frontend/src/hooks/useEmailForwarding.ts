import { useContext } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { isForbiddenError } from "@/api/forbidden";
import type {
  ForwardedEmail,
  ForwardingAddress,
  MintForwardingAddress,
} from "@/api/emailForwarding";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useActiveOrgId } from "@/store/ActiveOrgContext";
import CloudContext from "@/store/CloudContext";

const ROOT = "/email-forwarding";

export function useEmailForwardingAvailability(): boolean | undefined {
  const isCloud = useContext(CloudContext);
  const credentialGetter = useCredentialGetter();
  const orgId = useActiveOrgId();
  const { data, isPending } = useQuery({
    queryKey: ["emailForwarding", orgId, "availability"],
    queryFn: async () =>
      (
        await (
          await getClient(credentialGetter, "v1")
        ).get<{ available: boolean }>(`${ROOT}/availability`)
      ).data.available,
    enabled: isCloud,
    retry: false,
    retryOnMount: false,
    staleTime: Infinity,
  });
  if (!isCloud) return true;
  if (isPending) return undefined;
  return data === true;
}

export function useEmailForwardingAvailable(): boolean {
  return useEmailForwardingAvailability() === true;
}

export function useForwardingAddresses(enabled = true) {
  const credentialGetter = useCredentialGetter();
  const orgId = useActiveOrgId();
  const available = useEmailForwardingAvailable();
  return useQuery({
    queryKey: ["emailForwarding", orgId, "addresses"],
    queryFn: async () =>
      (
        await (
          await getClient(credentialGetter, "sans-api-v1")
        ).get<ForwardingAddress[]>(`${ROOT}/addresses`)
      ).data,
    enabled: available && enabled,
    refetchInterval: (query) =>
      isForbiddenError(query.state.error) ? false : 10_000,
    retry: false,
  });
}

export function useForwardingInbox(addressId: string, offset: number) {
  const credentialGetter = useCredentialGetter();
  const orgId = useActiveOrgId();
  const available = useEmailForwardingAvailable();
  return useQuery({
    queryKey: ["emailForwarding", orgId, "inbox", addressId, offset],
    queryFn: async () => {
      const response = await (
        await getClient(credentialGetter, "sans-api-v1")
      ).get<ForwardedEmail[]>(`${ROOT}/addresses/${addressId}/emails`, {
        params: { limit: 20, offset },
      });
      return {
        emails: response.data,
        hasMore: response.headers?.["x-has-more"] === "true",
      };
    },
    enabled: available,
    refetchInterval: (query) =>
      offset === 0 && !isForbiddenError(query.state.error) ? 10_000 : false,
    retry: false,
  });
}

type AddressAction =
  | { kind: "mint"; input: MintForwardingAddress }
  | {
      kind: "rotate";
      address: ForwardingAddress;
      managed: boolean;
      replacement?: string;
    }
  | { kind: "enable" | "disable" | "revoke"; address: ForwardingAddress };

export function useForwardingAction() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const orgId = useActiveOrgId();
  return useMutation({
    mutationFn: async (
      action: AddressAction,
    ): Promise<ForwardingAddress | null> => {
      const managed =
        action.kind === "mint" || (action.kind === "rotate" && action.managed);
      const client = await getClient(
        credentialGetter,
        managed ? "v1" : "sans-api-v1",
      );
      if (action.kind === "mint")
        return (
          await client.post<ForwardingAddress>(
            `${ROOT}/addresses/managed`,
            action.input,
          )
        ).data;
      const path = `${ROOT}/addresses/${action.address.forwarding_address_id}`;
      if (action.kind === "revoke") {
        await client.delete(path);
        return null;
      }
      if (action.kind === "rotate") {
        const response = await client.post<{ replacement: ForwardingAddress }>(
          `${path}/${action.managed ? "rotate-managed" : "rotate"}`,
          action.managed ? undefined : { email_address: action.replacement },
        );
        return response.data.replacement;
      }
      return (await client.post<ForwardingAddress>(`${path}/${action.kind}`))
        .data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ["emailForwarding", orgId],
      });
      await queryClient.invalidateQueries({ queryKey: ["credentials"] });
    },
  });
}
