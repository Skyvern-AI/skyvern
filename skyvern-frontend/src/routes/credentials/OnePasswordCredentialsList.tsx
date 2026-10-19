import { OnePasswordIcon } from "@/components/icons/OnePasswordIcon";
import { isAxiosError } from "axios";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import {
  ONEPASSWORD_CREDENTIALS_REQUEST_LIMIT,
  type OnePasswordCredentialRefreshResponse,
} from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { Button } from "@/components/ui/button";
import CloudContext from "@/store/CloudContext";
import { getHostname } from "@/util/getHostname";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useOnePasswordItemsQuery } from "@/routes/workflows/hooks/useOnePasswordItemsQuery";
import { useContext, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ImportOnePasswordDialog } from "./ImportOnePasswordDialog";
import { useImportedOnePasswordCredentialsQuery } from "./hooks/useImportedOnePasswordCredentialsQuery";
import { isOnePasswordItemOfType } from "./onePasswordItems";

type Props = {
  search?: string;
  folderId?: string | null;
};

function OnePasswordCredentialsList({ search, folderId }: Props) {
  const isCloud = useContext(CloudContext);
  const { data, isLoading, isError } = useOnePasswordItemsQuery();
  const {
    data: importedCredentials = [],
    isLoading: isLoadingImported,
    isError: isErrorImported,
  } = useImportedOnePasswordCredentialsQuery(!folderId);
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [refreshMessage, setRefreshMessage] = useState<string | null>(null);
  const [refreshHasErrors, setRefreshHasErrors] = useState(false);
  const refreshMutation = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const countsByFolder = new Map<string | null, number>();
      for (const credential of importedCredentials) {
        const folderId = credential.folder_id ?? null;
        countsByFolder.set(folderId, (countsByFolder.get(folderId) ?? 0) + 1);
      }

      const outcomes = await Promise.all(
        [...countsByFolder].map(async ([folderId, count]) => {
          // shortcut: same-folder batches above 500 are skipped, upgrade when refresh accepts item IDs.
          if (count > ONEPASSWORD_CREDENTIALS_REQUEST_LIMIT) {
            return { errorStatus: 400 };
          }
          try {
            const response =
              await client.post<OnePasswordCredentialRefreshResponse>(
                "/credentials/onepassword/refresh",
                { folder_id: folderId },
              );
            return { data: response.data };
          } catch (error) {
            return {
              errorStatus: isAxiosError(error)
                ? error.response?.status
                : undefined,
            };
          }
        }),
      );
      const responses = outcomes.flatMap((outcome) =>
        "data" in outcome && outcome.data ? [outcome.data] : [],
      );
      const errors = outcomes.flatMap((outcome) =>
        "errorStatus" in outcome ? [outcome.errorStatus] : [],
      );
      const results = responses.flatMap((response) => response.results);

      return {
        response: {
          refreshed: responses.reduce(
            (total, response) => total + response.refreshed,
            0,
          ),
          stale: responses.reduce(
            (total, response) => total + response.stale,
            0,
          ),
          skipped: responses.reduce(
            (total, response) => total + response.skipped,
            0,
          ),
          results,
        },
        errors,
        successfulBatches: responses.length,
      };
    },
    onSuccess: ({ response, errors, successfulBatches }) => {
      const errorMessages = [
        errors.includes(403) &&
          "Some folders could not be refreshed because you don’t have access.",
        errors.includes(400) &&
          "Some folders could not be refreshed because they contain more than 500 linked credentials.",
        errors.some((status) => status !== 400 && status !== 403) &&
          "Some folders could not be refreshed.",
      ].filter(Boolean);
      setRefreshMessage(
        `${response.refreshed} refreshed, ${response.stale} missing, ${response.skipped} skipped.${
          errorMessages.length ? ` ${errorMessages.join(" ")}` : ""
        }`,
      );
      setRefreshHasErrors(errors.length > 0);
      if (successfulBatches > 0) {
        void queryClient.invalidateQueries({ queryKey: ["credentials"] });
      }
    },
    onError: () => {
      setRefreshHasErrors(true);
      setRefreshMessage("Couldn’t refresh 1Password credentials.");
    },
  });
  const normalizedSearch = search?.trim().toLowerCase() ?? "";

  const passwordItems = useMemo(
    () =>
      (data?.items ?? []).filter((item) =>
        isOnePasswordItemOfType(item, "password"),
      ),
    [data?.items],
  );
  const filteredItems = useMemo(
    () =>
      passwordItems.filter(
        (item) =>
          !normalizedSearch ||
          item.title.toLowerCase().includes(normalizedSearch),
      ),
    [passwordItems, normalizedSearch],
  );
  const importedKeys = useMemo(
    () =>
      new Set(
        importedCredentials.flatMap((credential) =>
          credential.vault_id && credential.item_id
            ? [JSON.stringify([credential.vault_id, credential.item_id])]
            : [],
        ),
      ),
    [importedCredentials],
  );
  const hasOrganizationAccount =
    data?.configured && data.source === "organization";
  const hasImportableItems = (data?.items ?? []).some(
    (item) => !importedKeys.has(JSON.stringify([item.vault_id, item.item_id])),
  );
  const canImport =
    hasOrganizationAccount &&
    !isLoadingImported &&
    !isErrorImported &&
    hasImportableItems;
  const canRefresh = hasOrganizationAccount && importedCredentials.length > 0;
  const showHeaderImport = canImport && passwordItems.length > 0;

  const header = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex min-w-0 items-center gap-2">
        <OnePasswordIcon className="size-5 shrink-0" />
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-medium">1Password</p>
          <p className="text-sm text-neutral-600 dark:text-slate-400">
            Read-only — managed in your 1Password account
          </p>
        </div>
      </div>
      {showHeaderImport || canRefresh ? (
        <div className="flex flex-wrap items-center gap-2">
          {showHeaderImport && (
            <ImportOnePasswordDialog
              trigger={
                <Button variant="secondary" size="sm">
                  Import from 1Password
                </Button>
              }
            />
          )}
          {canRefresh && (
            <Button
              variant="outline"
              size="sm"
              disabled={refreshMutation.isPending}
              onClick={() => {
                setRefreshMessage(null);
                setRefreshHasErrors(false);
                refreshMutation.mutate();
              }}
            >
              <ReloadIcon
                className={`mr-2 size-3.5 ${refreshMutation.isPending ? "animate-spin" : ""}`}
              />
              {refreshMutation.isPending
                ? "Refreshing…"
                : "Refresh from 1Password"}
            </Button>
          )}
        </div>
      ) : null}
    </div>
  );

  if (folderId || isLoading) {
    return null;
  }

  if (isError) {
    return (
      <div className="space-y-4">
        {header}
        <div className="rounded-lg bg-slate-elevation2 p-4 text-sm text-neutral-600 dark:text-slate-400">
          Couldn&apos;t load 1Password items.
        </div>
      </div>
    );
  }

  if (data?.source === "instance_default") {
    return (
      <div className="space-y-4">
        {header}
        <p className="rounded-lg bg-slate-elevation2 p-4 text-sm text-neutral-600 dark:text-slate-400">
          Using the instance default 1Password account. Items cannot be listed
          here; enter a vault ID and item ID in the login block instead.
        </p>
      </div>
    );
  }

  if (
    data?.configured &&
    data.source === "organization" &&
    passwordItems.length === 0
  ) {
    return (
      <div className="space-y-4">
        {header}
        {refreshMessage && (
          <p
            role={refreshHasErrors ? "alert" : "status"}
            className="text-sm text-muted-foreground"
          >
            {refreshMessage}
          </p>
        )}
        <div className="rounded-lg bg-slate-elevation2 p-4 text-sm text-neutral-600 dark:text-slate-400">
          <p>
            No login or password items were returned from readable 1Password
            vaults. Check this service account&apos;s vault access. If those
            vaults contain no login or password items, add one or check its
            token in{" "}
            <Link
              className="font-medium text-blue-600 underline underline-offset-2 dark:text-blue-400"
              to={isCloud ? "/integrations?query=1Password" : "/settings"}
            >
              1Password integration settings
            </Link>
            .
          </p>
          {canImport && (
            <div className="mt-3">
              <ImportOnePasswordDialog
                trigger={
                  <Button variant="secondary" size="sm">
                    Import from 1Password
                  </Button>
                }
              />
            </div>
          )}
        </div>
      </div>
    );
  }

  if (!data?.configured || filteredItems.length === 0) {
    return null;
  }

  return (
    <div className="space-y-4">
      {header}
      {refreshMessage && (
        <p
          role={refreshHasErrors ? "alert" : "status"}
          className="text-sm text-muted-foreground"
        >
          {refreshMessage}
        </p>
      )}
      <div className="space-y-5">
        {filteredItems.map((item) => (
          <div
            className="flex gap-5 rounded-lg bg-slate-elevation2 p-4"
            key={`${item.vault_id}:${item.item_id}`}
          >
            <div className="flex w-48 items-center gap-2">
              <OnePasswordIcon className="size-5 shrink-0" />
              <div className="min-w-0 space-y-1">
                <p className="truncate" title={item.title}>
                  {item.title}
                </p>
                <p className="text-sm text-neutral-600 dark:text-slate-400">
                  {item.vault_name}
                </p>
              </div>
            </div>
            <div className="flex gap-5 border-l pl-5">
              <div className="shrink-0 space-y-2">
                {item.url && (
                  <p className="text-sm text-neutral-600 dark:text-slate-400">
                    Website
                  </p>
                )}
                <p className="text-sm text-neutral-600 dark:text-slate-400">
                  Vault ID
                </p>
                <p className="text-sm text-neutral-600 dark:text-slate-400">
                  Item ID
                </p>
              </div>
              <div className="min-w-0 space-y-2">
                {item.url && (
                  <p className="truncate text-sm" title={item.url}>
                    {getHostname(item.url) ?? item.url}
                  </p>
                )}
                <p className="truncate text-sm" title={item.vault_id}>
                  {item.vault_id}
                </p>
                <p className="truncate text-sm" title={item.item_id}>
                  {item.item_id}
                </p>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export { OnePasswordCredentialsList };
