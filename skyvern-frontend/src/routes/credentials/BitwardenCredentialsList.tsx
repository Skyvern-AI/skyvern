import { BitwardenIcon } from "@/components/icons/BitwardenIcon";
import { getHostname } from "@/util/getHostname";
import { useBitwardenItemsQuery } from "@/routes/workflows/hooks/useBitwardenItemsQuery";

type Props = {
  search?: string;
  folderId?: string | null;
};

function BitwardenCredentialsList({ search, folderId }: Props) {
  const { data, isLoading, isError } = useBitwardenItemsQuery();
  const normalizedSearch = search?.trim().toLowerCase() ?? "";
  const muted = "text-sm text-neutral-600 dark:text-slate-400";
  const filteredItems = (data?.items ?? []).filter(
    (item) =>
      item.credential_type === "password" &&
      (!normalizedSearch ||
        item.title.toLowerCase().includes(normalizedSearch)),
  );
  const hasNonPasswordItems = (data?.items ?? []).some(
    (item) => item.credential_type !== "password",
  );

  const header = (
    <div className="flex items-center gap-2">
      <BitwardenIcon className="size-5 shrink-0" />
      <div className="min-w-0 space-y-1">
        <p className="text-sm font-medium">Bitwarden</p>
        <p className={muted}>Read-only — managed in your Bitwarden account</p>
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
          Couldn&apos;t load Bitwarden items.
        </div>
      </div>
    );
  }

  if (!data?.configured) {
    return null;
  }

  if (filteredItems.length === 0) {
    if (!hasNonPasswordItems) {
      return null;
    }
    return (
      <div className="space-y-4">
        {header}
        <p className={muted}>
          Credit cards and secrets are available in the workflow editor.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {header}
      <div className="space-y-5">
        {filteredItems.map((item) => {
          const rows = [
            item.url && {
              label: "Website",
              value: getHostname(item.url) ?? item.url,
            },
            item.collection_id && {
              label: "Collection ID",
              value: item.collection_id,
            },
            { label: "Item ID", value: item.item_id },
          ].filter(Boolean) as Array<{ label: string; value: string }>;

          return (
            <div
              className="flex gap-5 rounded-lg bg-slate-elevation2 p-4"
              key={item.item_id}
            >
              <div className="flex w-48 items-center gap-2">
                <BitwardenIcon className="size-5 shrink-0" />
                <div className="min-w-0 space-y-1">
                  <p className="truncate" title={item.title}>
                    {item.title}
                  </p>
                  {item.collection_id && (
                    <p
                      className={`truncate ${muted}`}
                      title={item.collection_id}
                    >
                      {item.collection_id}
                    </p>
                  )}
                </div>
              </div>
              <div className="flex gap-5 border-l pl-5">
                {rows.map((row) => (
                  <div className="min-w-0 space-y-2" key={row.label}>
                    <p className={muted}>{row.label}</p>
                    <p className="truncate text-sm" title={row.value}>
                      {row.value}
                    </p>
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
      {hasNonPasswordItems && (
        <p className={muted}>
          Credit cards and secrets are available in the workflow editor.
        </p>
      )}
    </div>
  );
}

export { BitwardenCredentialsList };
