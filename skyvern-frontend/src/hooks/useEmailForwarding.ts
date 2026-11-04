import { useContext, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { getClient } from "@/api/AxiosClient";
import { isForbiddenError } from "@/api/forbidden";
import { isTransientNetworkError } from "@/api/transientNetworkError";
import type {
  ForwardedEmail,
  ForwardingAddress,
  MintForwardingAddress,
} from "@/api/emailForwarding";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useActiveOrgId } from "@/store/ActiveOrgContext";
import CloudContext from "@/store/CloudContext";
import { useHasCapability } from "@/store/PageSlots";

const ROOT = "/email-forwarding";

export function useEmailForwardingAvailability(
  capabilityAllowed = true,
): boolean | undefined {
  const isCloud = useContext(CloudContext);
  const canList = useHasCapability("email_forwarding.list");
  const credentialGetter = useCredentialGetter();
  const orgId = useActiveOrgId();
  const { data, isPending } = useQuery({
    queryKey: ["emailForwarding", orgId, "availability"],
    queryFn: async () => {
      try {
        return (
          await (
            await getClient(credentialGetter, "v1")
          ).get<{ available: boolean }>(`${ROOT}/availability`)
        ).data.available;
      } catch (error) {
        if (
          isAxiosError(error) &&
          (error.response?.status === 404 ||
            (error.response?.status === 403 &&
              (error.response.data?.detail?.error_code ===
                "organization_admin_required" ||
                error.response.data?.detail?.reason_kind === "role")))
        ) {
          return false;
        }
        throw error;
      }
    },
    enabled: isCloud && capabilityAllowed && canList,
    retry: (failureCount, error) =>
      failureCount < 3 &&
      (isTransientNetworkError(error) ||
        (isAxiosError(error) && (error.response?.status ?? 0) >= 500)),
    staleTime: Infinity,
  });
  if (!isCloud) return true;
  if (!capabilityAllowed || !canList) return false;
  if (isPending) return undefined;
  return data === true;
}

export function useEmailForwardingAvailable(): boolean {
  return useEmailForwardingAvailability() === true;
}

export type ForwardingCapability =
  | "list"
  | "read"
  | "create"
  | "update"
  | "delete";

export function useForwardingCapability(action: ForwardingCapability): boolean {
  const allowed = useHasCapability("email_forwarding." + action);
  const available = useEmailForwardingAvailability(allowed);
  return available === true && allowed;
}

export function useForwardingAddresses(enabled = true) {
  const credentialGetter = useCredentialGetter();
  const orgId = useActiveOrgId();
  const canList = useForwardingCapability("list");
  return useQuery({
    queryKey: ["emailForwarding", orgId, "addresses"],
    queryFn: async () =>
      (
        await (
          await getClient(credentialGetter, "sans-api-v1")
        ).get<ForwardingAddress[]>(`${ROOT}/addresses`)
      ).data,
    enabled: canList && enabled,
    refetchInterval: (query) =>
      isForbiddenError(query.state.error) ? false : 10_000,
    retry: false,
  });
}

type InboxWindow = {
  emails: ForwardedEmail[];
  hasMore: boolean;
  nextCursor: string | undefined;
};

type InboxPage = InboxWindow & { firstWindow: InboxWindow };

function hasLaterWindowEmails(page: InboxPage | undefined): boolean {
  return !!page && page.emails.length > page.firstWindow.emails.length;
}

export function useForwardingInbox(
  addressId: string,
  cursor: string,
  enabled = true,
) {
  const credentialGetter = useCredentialGetter();
  const orgId = useActiveOrgId();
  const canRead = useForwardingCapability("read");
  const queryClient = useQueryClient();
  const forceRefresh = useRef(false);
  const query = useQuery({
    queryKey: ["emailForwarding", orgId, "inbox", addressId, cursor],
    queryFn: async ({ signal, queryKey }): Promise<InboxPage> => {
      const previous = forceRefresh.current
        ? undefined
        : queryClient.getQueryData<InboxPage>(queryKey);
      forceRefresh.current = false;
      const client = await getClient(credentialGetter, "sans-api-v1");
      const seenCursors = new Set([cursor]);
      const readWindow = async (
        offset: string,
        limit: number,
      ): Promise<InboxWindow> => {
        const response = await client.get<ForwardedEmail[]>(
          `${ROOT}/addresses/${addressId}/emails`,
          { params: { limit, offset }, signal },
        );
        const hasMore = response.headers?.["x-has-more"] === "true";
        const nextCursor = hasMore
          ? (response.headers?.["x-next-cursor"] as string | undefined)
          : undefined;
        if (hasMore && (!nextCursor || seenCursors.has(nextCursor))) {
          throw new Error("Invalid inbox continuation");
        }
        if (nextCursor) seenCursors.add(nextCursor);
        return { emails: response.data, hasMore, nextCursor };
      };
      const firstWindow = await readWindow(cursor, 20);
      // Refresh only the newest window; keep scan progress if no later mail was cached.
      if (previous) {
        if (
          previous.hasMore &&
          JSON.stringify(firstWindow) ===
            JSON.stringify(previous.firstWindow) &&
          JSON.stringify(firstWindow.emails) === JSON.stringify(previous.emails)
        ) {
          return previous;
        }
        return { ...firstWindow, firstWindow };
      }
      const emails = [...firstWindow.emails];
      let currentWindow = firstWindow;
      for (
        let requests = 1;
        requests < 3 && emails.length < 20 && currentWindow.nextCursor;
        requests++
      ) {
        currentWindow = await readWindow(
          currentWindow.nextCursor,
          20 - emails.length,
        );
        emails.push(...currentWindow.emails);
      }
      return {
        emails,
        hasMore: currentWindow.hasMore,
        nextCursor: currentWindow.nextCursor,
        firstWindow,
      };
    },
    enabled: canRead && enabled && cursor !== "",
    staleTime: (query) =>
      hasLaterWindowEmails(query.state.data) ? Infinity : 0,
    refetchOnMount: (query) => !hasLaterWindowEmails(query.state.data),
    refetchOnWindowFocus: (query) => !hasLaterWindowEmails(query.state.data),
    refetchOnReconnect: (query) => !hasLaterWindowEmails(query.state.data),
    refetchInterval: (query) =>
      cursor === "0" &&
      !isForbiddenError(query.state.error) &&
      query.state.data &&
      !hasLaterWindowEmails(query.state.data)
        ? 10_000
        : false,
    retry: false,
  });
  return {
    ...query,
    refreshNewest: () => {
      queryClient.removeQueries({
        queryKey: ["emailForwarding", orgId, "inbox", addressId],
        predicate: (cached) => cursor !== "0" || cached.queryKey[4] !== "0",
      });
      if (cursor === "0") {
        forceRefresh.current = true;
        return query.refetch();
      }
    },
  };
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
