import type { OnePasswordItemApiResponse } from "@/api/types";
import { OnePasswordIcon } from "@/components/icons/OnePasswordIcon";
import { getHostname } from "@/util/getHostname";
import { useOnePasswordItemsQuery } from "@/routes/workflows/hooks/useOnePasswordItemsQuery";
import { useMemo } from "react";

type Props = {
  search?: string;
  folderId?: string | null;
};

function isPasswordItem(item: OnePasswordItemApiResponse) {
  const category = item.category.toLowerCase();
  return category.includes("login") || category.includes("password");
}

function OnePasswordCredentialsList({ search, folderId }: Props) {
  const { data, isLoading, isError } = useOnePasswordItemsQuery();
  const normalizedSearch = search?.trim().toLowerCase() ?? "";

  const filteredItems = useMemo(() => {
    return (data?.items ?? []).filter((item) => {
      if (!isPasswordItem(item)) {
        return false;
      }

      if (!normalizedSearch) {
        return true;
      }

      return item.title.toLowerCase().includes(normalizedSearch);
    });
  }, [data?.items, normalizedSearch]);

  const header = (
    <div className="flex items-center gap-2">
      <OnePasswordIcon className="size-5 shrink-0" />
      <div className="min-w-0 space-y-1">
        <p className="text-sm font-medium">1Password</p>
        <p className="text-sm text-neutral-600 dark:text-slate-400">
          Read-only — managed in your 1Password account
        </p>
      </div>
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

  if (!data?.configured || filteredItems.length === 0) {
    return null;
  }

  return (
    <div className="space-y-4">
      {header}
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
