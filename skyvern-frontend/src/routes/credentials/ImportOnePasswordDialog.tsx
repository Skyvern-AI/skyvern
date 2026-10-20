import { useMemo, useState, type ReactElement } from "react";
import { isAxiosError } from "axios";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import {
  ONEPASSWORD_CREDENTIALS_REQUEST_LIMIT,
  type OnePasswordCredentialImportRequest,
  type OnePasswordCredentialImportResponse,
  type OnePasswordCredentialImportResult,
} from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useOnePasswordItemsQuery } from "@/routes/workflows/hooks/useOnePasswordItemsQuery";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { getCredentialErrorMessage } from "./authenticatorSaveError";
import { CredentialFolderSelector } from "./CredentialFolderSelector";
import { useImportedOnePasswordCredentialsQuery } from "./hooks/useImportedOnePasswordCredentialsQuery";
import type { CredentialFolder } from "./types/credentialFolderTypes";

type Props = {
  trigger: ReactElement;
};

type VaultGroup = {
  vaultId: string;
  vaultName: string;
  items: Array<{
    item_id: string;
    title: string;
    category: string;
    vault_id: string;
  }>;
};

function itemKey(vaultId: string, itemId: string): string {
  return JSON.stringify([vaultId, itemId]);
}

function resultLabel(
  status: OnePasswordCredentialImportResult["status"],
): string {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function ImportOnePasswordDialog({ trigger }: Props) {
  const [open, setOpen] = useState(false);
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [targetFolder, setTargetFolder] = useState<CredentialFolder | null>(
    null,
  );
  const [results, setResults] =
    useState<Array<OnePasswordCredentialImportResult> | null>(null);
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const itemsQuery = useOnePasswordItemsQuery({ enabled: open });
  const importedCredentialsQuery = useImportedOnePasswordCredentialsQuery(open);
  const importedKeys = useMemo(
    () =>
      new Set(
        (importedCredentialsQuery.data ?? [])
          .filter((credential) => credential.vault_type === "onepassword")
          .flatMap((credential) =>
            credential.vault_id && credential.item_id
              ? [itemKey(credential.vault_id, credential.item_id)]
              : [],
          ),
      ),
    [importedCredentialsQuery.data],
  );
  const items = useMemo(
    () => itemsQuery.data?.items ?? [],
    [itemsQuery.data?.items],
  );
  const groups = useMemo(() => {
    const byVault = new Map<string, VaultGroup>();
    for (const item of items) {
      let group = byVault.get(item.vault_id);
      if (!group) {
        group = {
          vaultId: item.vault_id,
          vaultName: item.vault_name,
          items: [],
        };
        byVault.set(item.vault_id, group);
      }
      group.items.push(item);
    }
    return [...byVault.values()];
  }, [items]);
  const canSelect =
    !itemsQuery.isLoading &&
    !itemsQuery.isError &&
    !importedCredentialsQuery.isLoading &&
    !importedCredentialsQuery.isError;
  const selectedItems = items.filter(
    (item) =>
      selectedKeys.has(itemKey(item.vault_id, item.item_id)) &&
      !importedKeys.has(itemKey(item.vault_id, item.item_id)),
  );
  const importMutation = useMutation({
    mutationFn: async (request: OnePasswordCredentialImportRequest) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const importItems = async (
        items: OnePasswordCredentialImportRequest["items"],
      ) =>
        (
          await client.post<OnePasswordCredentialImportResponse>(
            "/credentials/onepassword/import",
            { ...request, items },
          )
        ).data;

      try {
        return await importItems(request.items);
      } catch (error) {
        if (!isAxiosError(error) || error.response?.status !== 403) throw error;

        const results: Array<OnePasswordCredentialImportResult> = [];
        let deniedCount = 0;
        for (const item of request.items) {
          try {
            results.push(...(await importItems([item])).results);
          } catch (itemError) {
            if (
              !isAxiosError(itemError) ||
              itemError.response?.status !== 403
            ) {
              throw itemError;
            }
            deniedCount += 1;
            results.push({
              ...item,
              status: "skipped",
              reason: "Not permitted",
            });
          }
        }
        if (deniedCount === request.items.length) throw error;
        return { results };
      }
    },
    onSuccess: ({ results: importResults }) => {
      setResults(importResults);
      setSelectedKeys(new Set());
      void queryClient.invalidateQueries({ queryKey: ["credentials"] });
    },
  });
  const resultNames = useMemo(
    () =>
      new Map(
        items.map((item) => [itemKey(item.vault_id, item.item_id), item.title]),
      ),
    [items],
  );

  const handleOpenChange = (nextOpen: boolean) => {
    setOpen(nextOpen);
    if (nextOpen) {
      setSelectedKeys(new Set());
      setTargetFolder(null);
      setResults(null);
      importMutation.reset();
    }
  };

  const handleImport = () => {
    if (!selectedItems.length) return;
    const request: OnePasswordCredentialImportRequest = {
      ...(targetFolder ? { folder_id: targetFolder.folder_id } : {}),
      items: selectedItems.map(({ vault_id, item_id }) => ({
        vault_id,
        item_id,
      })),
    };
    importMutation.mutate(request);
  };

  const loading = itemsQuery.isLoading || importedCredentialsQuery.isLoading;
  const loadError = itemsQuery.isError
    ? "Couldn’t load 1Password items."
    : importedCredentialsQuery.isError
      ? "Couldn’t check which items are already imported."
      : null;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="ph-no-capture max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Import from 1Password</DialogTitle>
          <DialogDescription>
            Select items to add to your credentials. Imported items stay managed
            in 1Password.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm font-medium">Target folder</p>
            <p className="text-xs text-muted-foreground">
              {targetFolder?.title ?? "No folder"}
            </p>
          </div>
          <CredentialFolderSelector
            currentFolderId={targetFolder?.folder_id ?? null}
            onFolderSelect={setTargetFolder}
            trigger={
              <Button variant="outline" size="sm">
                {targetFolder?.title ?? "No folder"}
              </Button>
            }
          />
        </div>

        {loading ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            Loading 1Password items…
          </p>
        ) : loadError ? (
          <p role="alert" className="rounded-md bg-destructive/10 p-3 text-sm">
            {loadError}
          </p>
        ) : itemsQuery.data?.source !== "organization" ? (
          <p className="rounded-md bg-slate-elevation2 p-3 text-sm text-muted-foreground">
            An organization 1Password account is required to import items.
          </p>
        ) : groups.length === 0 ? (
          <p className="rounded-md bg-slate-elevation2 p-3 text-sm text-muted-foreground">
            No items were found in readable 1Password vaults.
          </p>
        ) : (
          <div className="max-h-[48vh] space-y-4 overflow-y-auto pr-1">
            {groups.map((group) => {
              const selectableItems = group.items.filter(
                (item) =>
                  !importedKeys.has(itemKey(item.vault_id, item.item_id)),
              );
              const selectedCount = selectableItems.filter((item) =>
                selectedKeys.has(itemKey(item.vault_id, item.item_id)),
              ).length;
              const allChecked =
                selectedCount === selectableItems.length &&
                selectableItems.length > 0;
              const groupChecked = allChecked
                ? true
                : selectedCount > 0
                  ? "indeterminate"
                  : false;

              return (
                <section
                  key={group.vaultId}
                  className="rounded-lg border border-slate-700"
                  aria-label={group.vaultName}
                >
                  <div className="flex items-center gap-3 border-b border-slate-700 px-4 py-3">
                    <Checkbox
                      aria-label={`Select all in ${group.vaultName}`}
                      checked={groupChecked}
                      disabled={
                        !canSelect ||
                        importMutation.isPending ||
                        selectableItems.length === 0 ||
                        (selectedKeys.size >=
                          ONEPASSWORD_CREDENTIALS_REQUEST_LIMIT &&
                          selectedCount === 0)
                      }
                      onCheckedChange={(checked) =>
                        setSelectedKeys((current) => {
                          const next = new Set(current);
                          for (const item of selectableItems) {
                            const key = itemKey(item.vault_id, item.item_id);
                            if (checked === true) {
                              if (
                                next.size <
                                ONEPASSWORD_CREDENTIALS_REQUEST_LIMIT
                              ) {
                                next.add(key);
                              }
                            } else {
                              next.delete(key);
                            }
                          }
                          return next;
                        })
                      }
                    />
                    <h3 className="text-sm font-medium">{group.vaultName}</h3>
                    <span className="ml-auto text-xs text-muted-foreground">
                      {group.items.length}{" "}
                      {group.items.length === 1 ? "item" : "items"}
                    </span>
                  </div>
                  <div className="divide-y divide-slate-700/70">
                    {group.items.map((item) => {
                      const key = itemKey(item.vault_id, item.item_id);
                      const isImported = importedKeys.has(key);
                      return (
                        <label
                          key={key}
                          className="flex cursor-pointer items-start gap-3 px-4 py-3 has-[:disabled]:cursor-not-allowed"
                        >
                          <Checkbox
                            aria-label={`Select ${item.title}`}
                            checked={selectedKeys.has(key)}
                            disabled={
                              !canSelect ||
                              isImported ||
                              importMutation.isPending ||
                              (selectedKeys.size >=
                                ONEPASSWORD_CREDENTIALS_REQUEST_LIMIT &&
                                !selectedKeys.has(key))
                            }
                            onCheckedChange={(checked) =>
                              setSelectedKeys((current) => {
                                const next = new Set(current);
                                if (checked === true) next.add(key);
                                else next.delete(key);
                                return next;
                              })
                            }
                          />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-sm font-medium">
                              {item.title}
                            </span>
                            <span className="text-xs text-muted-foreground">
                              {item.category}
                              {isImported ? " · Already imported" : ""}
                            </span>
                          </span>
                        </label>
                      );
                    })}
                  </div>
                </section>
              );
            })}
          </div>
        )}

        {importMutation.isError && (
          <p role="alert" className="text-sm text-destructive">
            {isAxiosError(importMutation.error) &&
            importMutation.error.response?.status === 403
              ? "You don’t have permission to import these 1Password items. Choose an accessible folder or contact your administrator."
              : (getCredentialErrorMessage(importMutation.error) ??
                "Failed to import 1Password items.")}
          </p>
        )}
        {results && (
          <section aria-label="Import results" className="space-y-2">
            <h3 className="text-sm font-medium">Import results</h3>
            <ul className="space-y-2">
              {results.map((result) => (
                <li
                  key={itemKey(result.vault_id, result.item_id)}
                  className="flex flex-wrap items-start gap-x-3 gap-y-1 rounded-md bg-slate-elevation2 px-3 py-2 text-sm"
                >
                  <span className="font-medium">
                    {resultNames.get(
                      itemKey(result.vault_id, result.item_id),
                    ) ?? result.item_id}
                  </span>
                  <span className="text-muted-foreground">
                    {resultLabel(result.status)}
                  </span>
                  {result.reason && (
                    <span className="basis-full text-xs text-muted-foreground">
                      {result.reason}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

        <DialogFooter className="flex-row items-center justify-between sm:justify-between">
          <span className="text-xs text-muted-foreground">
            {selectedItems.length} selected
            {selectedItems.length === ONEPASSWORD_CREDENTIALS_REQUEST_LIMIT &&
              items.length > ONEPASSWORD_CREDENTIALS_REQUEST_LIMIT &&
              " · Maximum per import"}
          </span>
          <Button
            onClick={handleImport}
            disabled={
              !canSelect ||
              selectedItems.length === 0 ||
              importMutation.isPending
            }
          >
            {importMutation.isPending ? "Importing…" : "Import selected items"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { ImportOnePasswordDialog };
