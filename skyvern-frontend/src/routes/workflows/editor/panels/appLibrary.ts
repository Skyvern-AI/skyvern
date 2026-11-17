import type {
  IntegrationActionSpec,
  IntegrationCatalogEntryWithActions,
} from "../nodes/IntegrationActionNode/types";

export type AppRow = {
  entry: IntegrationCatalogEntryWithActions;
  connected: boolean;
};

export type AppActionMatch = AppRow & { action: IntegrationActionSpec };

export function appRows(
  catalog: readonly IntegrationCatalogEntryWithActions[] | undefined,
  isConnected: (entry: IntegrationCatalogEntryWithActions) => boolean,
): AppRow[] {
  const rows = (catalog ?? [])
    // The integration action block cannot run on a Nango-only account yet, so those apps offer no actions.
    .map((entry) =>
      entry.credentialBackend === "nango" && !entry.oauthProvider
        ? { ...entry, actions: [] }
        : entry,
    )
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

export function appRowMeta({ entry, connected }: AppRow): string {
  const count = (n: number, word: string) =>
    n ? `${n} ${word}${n === 1 ? "" : "s"}` : null;
  return [
    connected ? "Connected" : null,
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
