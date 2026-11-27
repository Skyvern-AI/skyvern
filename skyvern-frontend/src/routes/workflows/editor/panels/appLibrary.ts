import {
  isGenericActionAvailable,
  type IntegrationActionSpec,
  type IntegrationCatalogEntryWithActions,
} from "../nodes/IntegrationActionNode/types";

export type AppRow = {
  entry: IntegrationCatalogEntryWithActions;
  // Undefined until the account list has loaded: an empty list while loading or after an error proves nothing.
  connected: boolean | undefined;
};

export type AppActionMatch = AppRow & { action: IntegrationActionSpec };

export function appRows(
  catalog: readonly IntegrationCatalogEntryWithActions[] | undefined,
  isConnected: (
    entry: IntegrationCatalogEntryWithActions,
  ) => boolean | undefined,
): AppRow[] {
  const rows = (catalog ?? [])
    // Actions pinned to a Nango account are not offered by this generic block.
    .map((entry) => ({
      ...entry,
      actions: entry.actions?.filter(isGenericActionAvailable),
    }))
    .filter(
      (entry) =>
        entry.status === "live" &&
        ((entry.actions?.length ?? 0) > 0 || (entry.triggers?.length ?? 0) > 0),
    )
    .map((entry) => ({ entry, connected: isConnected(entry) }));
  return [
    ...rows.filter((row) => row.connected),
    ...rows.filter((row) => !row.connected),
  ];
}

export function appAccountStatus({ entry, connected }: AppRow): string | null {
  if (connected) return "Account connected";
  return entry.oauthProvider && connected === false
    ? "No account connected"
    : null;
}

export function appRowMeta(row: AppRow): string {
  const { entry } = row;
  const count = (n: number, word: string) =>
    n ? `${n} ${word}${n === 1 ? "" : "s"}` : null;
  return [
    appAccountStatus(row),
    count(entry.actions?.length ?? 0, "action"),
    count(entry.triggers?.length ?? 0, "trigger"),
  ]
    .filter(Boolean)
    .join(" · ");
}

// Every word must appear in "<app> <action>", so "send email" finds each app's send action.
export function matchingAppActions(
  rows: readonly AppRow[],
  search: string,
): AppActionMatch[] {
  const words = search.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  return rows.flatMap((row) =>
    (row.entry.actions ?? [])
      .filter((action) => {
        const text = `${row.entry.name} ${action.name}`.toLowerCase();
        return words.every((word) => text.includes(word));
      })
      .map((action) => ({ ...row, action })),
  );
}
