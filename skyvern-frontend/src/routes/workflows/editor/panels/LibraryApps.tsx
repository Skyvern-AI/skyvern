import { useState } from "react";

import {
  ChevronDownIcon,
  ChevronRightIcon,
  PlusIcon,
} from "@radix-ui/react-icons";

import {
  hasGoogleOAuthCredentialScopes,
  isGoogleOAuthCredentialActive,
  useGoogleOAuthCredentials,
} from "@/hooks/useGoogleOAuthCredentials";
import {
  hasMicrosoftOAuthCredentialScopes,
  isMicrosoftOAuthCredentialActive,
  useMicrosoftOAuthCredentials,
} from "@/hooks/useMicrosoftOAuthCredentials";

import {
  builtInBlockFor,
  integrationActionTitle,
  type IntegrationCatalogEntryWithActions,
} from "../nodes/IntegrationActionNode/types";
import { workflowBlockTitle } from "../nodes/types";
import { appRowMeta, appRows, matchingAppActions } from "./appLibrary";
import { useStudioPaneIds, useStudioPanes } from "../../studio/useStudioPanes";

function AppBadge({ name }: { name: string }) {
  return (
    <div
      aria-hidden
      className="flex h-[2.75rem] w-[2.75rem] shrink-0 items-center justify-center rounded border border-border text-sm font-semibold dark:border-slate-600"
    >
      {name.charAt(0).toUpperCase()}
    </div>
  );
}

type Props = {
  catalog: readonly IntegrationCatalogEntryWithActions[];
  search: string;
  onAdd: (integrationAction: {
    integrationId: string;
    actionId: string;
  }) => void;
};

// Search shows matching actions across apps; otherwise the Apps group with expandable rows.
function LibraryApps({ catalog, search, onAdd }: Props) {
  const hasActions = (provider: string) =>
    catalog.some(
      (entry) => entry.oauthProvider === provider && entry.actions?.length,
    );
  const google = useGoogleOAuthCredentials({ enabled: hasActions("google") });
  const microsoft = useMicrosoftOAuthCredentials({
    enabled: hasActions("microsoft"),
  });
  const [expandedApp, setExpandedApp] = useState<string | null>(null);
  // Triggers are not blocks: a trigger row opens the agent's Triggers pane, which only Cloud provides for a changeable agent.
  const canOpenTriggers = useStudioPaneIds().includes("triggers");
  const { openPane } = useStudioPanes();
  const openTriggers = () => openPane("triggers");
  const apps = appRows(catalog, (entry) => {
    const scopes = entry.capabilityScopes ?? entry.requiredScopes ?? [];
    if (entry.oauthProvider === "google") {
      return google.credentials.some(
        (credential) =>
          isGoogleOAuthCredentialActive(credential) &&
          hasGoogleOAuthCredentialScopes(credential, scopes),
      );
    }
    if (entry.oauthProvider === "microsoft") {
      return microsoft.credentials.some(
        (credential) =>
          isMicrosoftOAuthCredentialActive(credential) &&
          hasMicrosoftOAuthCredentialScopes(credential, scopes),
      );
    }
    return false;
  });

  if (search) {
    return (
      <>
        {matchingAppActions(apps, search).map(
          ({ entry, action, connected }) => (
            <button
              type="button"
              key={`${entry.id}:${action.id}`}
              data-testid={`block-library-app-action-${entry.id}-${action.id}`}
              className="flex w-full min-w-0 items-center justify-between gap-2 rounded-sm bg-slate-elevation4 p-4 text-left hover:bg-slate-elevation5"
              onClick={() =>
                onAdd({
                  integrationId: entry.id,
                  actionId: action.id,
                })
              }
            >
              <div className="flex min-w-0 gap-2">
                <AppBadge name={entry.name} />
                <div className="flex min-w-0 flex-col gap-1">
                  <span className="min-w-0 truncate text-base">
                    {integrationActionTitle(entry, action)}
                  </span>
                  <span className="min-w-0 truncate text-xs text-slate-600 dark:text-slate-400">
                    {connected
                      ? "Connected"
                      : entry.oauthProvider
                        ? "Connect when you add it"
                        : null}
                  </span>
                </div>
              </div>
              <PlusIcon className="size-6 shrink-0" />
            </button>
          ),
        )}
      </>
    );
  }
  return (
    <>
      <div className="pt-2 text-xs font-semibold uppercase tracking-wider text-slate-600 dark:text-slate-400">
        Apps
      </div>
      {apps.map((row) => {
        const { entry } = row;
        const actions = entry.actions ?? [];
        const triggers = canOpenTriggers ? (entry.triggers ?? []) : [];
        const canExpand = actions.length + triggers.length > 0;
        const expanded = canExpand && expandedApp === entry.id;
        return (
          <div
            key={entry.id}
            className="overflow-hidden rounded-sm bg-slate-elevation4"
          >
            <button
              type="button"
              disabled={!canExpand}
              aria-expanded={canExpand ? expanded : undefined}
              data-testid={`block-library-app-${entry.id}`}
              className="flex w-full min-w-0 items-center gap-2 p-4 text-left enabled:hover:bg-slate-elevation5 disabled:cursor-default"
              onClick={() => setExpandedApp(expanded ? null : entry.id)}
            >
              <AppBadge name={entry.name} />
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <span className="min-w-0 truncate text-base">{entry.name}</span>
                <span className="min-w-0 truncate text-xs text-slate-600 dark:text-slate-400">
                  {appRowMeta(row)}
                </span>
              </div>
              {canExpand ? (
                expanded ? (
                  <ChevronDownIcon className="size-4 shrink-0" />
                ) : (
                  <ChevronRightIcon className="size-4 shrink-0" />
                )
              ) : null}
            </button>
            {expanded ? (
              <>
                {actions.map((action) => {
                  const builtIn = builtInBlockFor(action);
                  return (
                    <button
                      type="button"
                      key={action.id}
                      data-testid={`block-library-app-action-${entry.id}-${action.id}`}
                      className="flex w-full min-w-0 items-center gap-2 border-t border-border py-2.5 pl-16 pr-4 text-left text-sm hover:bg-slate-elevation5 dark:border-slate-700"
                      onClick={() =>
                        onAdd({ integrationId: entry.id, actionId: action.id })
                      }
                    >
                      <span className="min-w-0 flex-1 truncate">
                        {action.name}
                      </span>
                      {builtIn ? (
                        <span className="shrink-0 text-xs text-slate-600 dark:text-slate-400">
                          {workflowBlockTitle[builtIn.blockType]} block
                        </span>
                      ) : null}
                      <PlusIcon className="size-5 shrink-0" />
                    </button>
                  );
                })}
                {triggers.map((trigger) => (
                  <button
                    type="button"
                    key={trigger.id}
                    data-testid={`block-library-app-trigger-${entry.id}-${trigger.id}`}
                    className="flex w-full min-w-0 items-center gap-2 border-t border-border py-2.5 pl-16 pr-4 text-left text-sm hover:bg-slate-elevation5 dark:border-slate-700"
                    onClick={openTriggers}
                  >
                    <span className="min-w-0 flex-1 truncate">
                      {trigger.name}
                    </span>
                    <span className="text-xs text-slate-600 dark:text-slate-400">
                      Trigger
                    </span>
                    <PlusIcon className="size-5 shrink-0" />
                  </button>
                ))}
              </>
            ) : null}
          </div>
        );
      })}
      <a
        href="/integrations"
        target="_blank"
        rel="noreferrer"
        className="block px-1 py-1 text-sm text-slate-600 underline-offset-4 hover:underline dark:text-slate-400"
      >
        Browse all apps
      </a>
    </>
  );
}

export { LibraryApps };
