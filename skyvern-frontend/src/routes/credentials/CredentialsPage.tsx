import { useCallback, useEffect, useMemo, useState } from "react";
import { useDebounce } from "use-debounce";
import { Button } from "@/components/ui/button";
import { TableSearchInput } from "@/components/TableSearchInput";
import { CardStackIcon, LockClosedIcon, PlusIcon } from "@radix-ui/react-icons";
import {
  CredentialModalTypes,
  useCredentialModalState,
} from "./useCredentialModalState";
import { CredentialsModal } from "./CredentialsModal";
import { CredentialsList } from "./CredentialsList";
import { useBackgroundCredentialTest } from "./useBackgroundCredentialTest";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { KeyIcon } from "@/components/icons/KeyIcon";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { CredentialsTotpTab } from "./CredentialsTotpTab";
import { useSearchParams } from "react-router-dom";
import { FolderIcon } from "@/components/icons/FolderIcon";
import { getUniqueSlugForFolder } from "@/util/folderSlug";
import { cn } from "@/util/utils";
import { useCredentialFoldersQuery } from "./hooks/useCredentialFoldersQuery";
import { CredentialFolderCard } from "./CredentialFolderCard";
import { CreateCredentialFolderDialog } from "./CreateCredentialFolderDialog";
import { ViewAllCredentialFoldersDialog } from "./ViewAllCredentialFoldersDialog";
import type { CredentialFolder } from "./types/credentialFolderTypes";

const subHeaderText =
  "Securely store your passwords, credit cards, secrets, and manage incoming 2FA codes for your agents.";

const TAB_VALUES = [
  "passwords",
  "creditCards",
  "secrets",
  "twoFactor",
] as const;
type TabValue = (typeof TAB_VALUES)[number];
const DEFAULT_TAB: TabValue = "passwords";

function CredentialsPage() {
  const { setIsOpen, setType } = useCredentialModalState();
  const { startBackgroundTest } = useBackgroundCredentialTest();
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 250);
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const matchedTab = TAB_VALUES.find((tab) => tab === tabParam);
  const activeTab: TabValue = matchedTab ?? DEFAULT_TAB;

  useEffect(() => {
    if (tabParam && !matchedTab) {
      const params = new URLSearchParams(searchParams);
      params.set("tab", DEFAULT_TAB);
      setSearchParams(params, { replace: true });
    }
  }, [tabParam, matchedTab, searchParams, setSearchParams]);

  function handleTabChange(value: string) {
    const nextTab = TAB_VALUES.find((tab) => tab === value) ?? DEFAULT_TAB;
    const params = new URLSearchParams(searchParams);
    params.set("tab", nextTab);
    setSearchParams(params, { replace: true });
  }

  const folderSlug = searchParams.get("folder");
  const [isCreateFolderOpen, setIsCreateFolderOpen] = useState(false);
  const [isViewAllFoldersOpen, setIsViewAllFoldersOpen] = useState(false);

  // Load a generous page so the slug map covers every folder a user can pick
  // from "View all" or a shared ?folder= URL, not just the recent few.
  const { data: allFolders = [], isLoading: isFoldersLoading } =
    useCredentialFoldersQuery({ page_size: 100 });

  const slugToFolderMap = useMemo(() => {
    const map = new Map<string, CredentialFolder>();
    for (const folder of allFolders) {
      map.set(getUniqueSlugForFolder(folder, allFolders), folder);
    }
    return map;
  }, [allFolders]);

  // selectedFolderId is derived from the ?folder= slug (handles collision suffixes)
  const selectedFolderId = useMemo(() => {
    if (!folderSlug) return null;
    const mapped = slugToFolderMap.get(folderSlug)?.folder_id;
    if (mapped) return mapped;
    // A ?folder= value that isn't a known slug may be the id of a folder past
    // the loaded page; treat a credential-folder id as the filter directly.
    return folderSlug.startsWith("cfld_") ? folderSlug : null;
  }, [folderSlug, slugToFolderMap]);

  // While a deep-linked ?folder= slug is still resolving, hold the credential
  // lists so the unfiltered bank doesn't flash before the filter applies.
  const isResolvingFolder = Boolean(folderSlug) && isFoldersLoading;

  // Drop a stale ?folder= slug (deleted/renamed folder) once folders have loaded
  useEffect(() => {
    if (
      folderSlug &&
      !selectedFolderId &&
      allFolders.length > 0 &&
      !isFoldersLoading
    ) {
      const params = new URLSearchParams(searchParams);
      params.delete("folder");
      setSearchParams(params, { replace: true });
    }
  }, [
    folderSlug,
    selectedFolderId,
    allFolders.length,
    isFoldersLoading,
    searchParams,
    setSearchParams,
  ]);

  // Writes the ?folder= slug while preserving the ?tab= param so the folder
  // filter persists across tab switches.
  const setSelectedFolderId = useCallback(
    (folderId: string | null) => {
      const params = new URLSearchParams(searchParams);
      if (folderId) {
        const folder = allFolders.find((f) => f.folder_id === folderId);
        // Fall back to the folder id as the slug when the folder is past the
        // loaded page, so selecting it from "View all" still deep-links.
        params.set(
          "folder",
          folder ? getUniqueSlugForFolder(folder, allFolders) : folderId,
        );
        setSearchParams(params, { replace: true });
        return;
      }
      params.delete("folder");
      setSearchParams(params, { replace: true });
    },
    [searchParams, allFolders, setSearchParams],
  );

  const recentFolders = useMemo(() => {
    return [...allFolders]
      .sort(
        (a, b) =>
          new Date(b.modified_at).getTime() - new Date(a.modified_at).getTime(),
      )
      .slice(0, 5);
  }, [allFolders]);

  return (
    <div className="space-y-5">
      <h1 className="text-2xl">Credentials</h1>
      <div className="flex items-center justify-between">
        <div className="w-96 text-sm text-neutral-600 dark:text-slate-300">
          {subHeaderText}
        </div>
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <Button>
              <PlusIcon className="mr-2 size-6" /> Add
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent className="w-48">
            <DropdownMenuItem
              onSelect={() => {
                setIsOpen(true);
                setType(CredentialModalTypes.PASSWORD);
              }}
              className="cursor-pointer"
            >
              <KeyIcon className="mr-2 size-4" />
              Password
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={() => {
                setIsOpen(true);
                setType(CredentialModalTypes.CREDIT_CARD);
              }}
              className="cursor-pointer"
            >
              <CardStackIcon className="mr-2 size-4" />
              Credit Card
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={() => {
                setIsOpen(true);
                setType(CredentialModalTypes.SECRET);
              }}
              className="cursor-pointer"
            >
              <LockClosedIcon className="mr-2 size-4" />
              Secret
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <div className={cn("space-y-4", activeTab === "twoFactor" && "hidden")}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold">Folders</h2>
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0 text-blue-600 dark:text-blue-400"
              onClick={() => setIsCreateFolderOpen(true)}
            >
              + New folder
            </Button>
          </div>
          {allFolders.length > 5 && (
            <Button
              variant="link"
              size="sm"
              className="text-blue-600 dark:text-blue-400"
              onClick={() => setIsViewAllFoldersOpen(true)}
            >
              View all
            </Button>
          )}
        </div>

        {isFoldersLoading ? (
          <div className="grid grid-cols-5 gap-4">
            {Array.from({ length: 5 }).map((_, i) => (
              <div
                key={i}
                className="h-24 animate-pulse rounded-lg border border-slate-200 bg-slate-elevation1 dark:border-slate-700"
              />
            ))}
          </div>
        ) : recentFolders.length > 0 ? (
          <div className="grid grid-cols-5 gap-4">
            {recentFolders.map((folder) => (
              <CredentialFolderCard
                key={folder.folder_id}
                folder={folder}
                isSelected={selectedFolderId === folder.folder_id}
                onClick={() =>
                  setSelectedFolderId(
                    selectedFolderId === folder.folder_id
                      ? null
                      : folder.folder_id,
                  )
                }
              />
            ))}
          </div>
        ) : (
          <div className="rounded-lg border border-slate-200 bg-slate-elevation1 py-6 text-center dark:border-slate-700">
            <div className="mx-auto max-w-md">
              <FolderIcon className="mx-auto mb-3 h-10 w-10 text-blue-400 opacity-50" />
              <h3 className="mb-2 text-slate-900 dark:text-slate-100">
                Organize Your Credentials with Folders
              </h3>
              <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">
                Keep your credentials organized by creating folders. Group
                related credentials together by project, team, or environment
                for easier management.
              </p>
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0 text-blue-600 dark:text-blue-400"
                onClick={() => setIsCreateFolderOpen(true)}
              >
                <PlusIcon className="mr-2 h-4 w-4" />
                Create Your First Folder
              </Button>
            </div>
          </div>
        )}
      </div>

      <Tabs
        value={activeTab}
        className="space-y-4"
        onValueChange={handleTabChange}
      >
        <div className="flex flex-wrap items-center justify-between gap-4">
          <TabsList className="bg-slate-elevation1">
            <TabsTrigger value="passwords">Passwords</TabsTrigger>
            <TabsTrigger value="creditCards">Credit Cards</TabsTrigger>
            <TabsTrigger value="secrets">Secrets</TabsTrigger>
            <TabsTrigger value="twoFactor">2FA</TabsTrigger>
          </TabsList>
          <div className="flex items-center gap-3">
            {selectedFolderId && activeTab !== "twoFactor" && (
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0 text-blue-600 dark:text-blue-400"
                onClick={() => setSelectedFolderId(null)}
              >
                View all credentials
              </Button>
            )}
            {activeTab !== "twoFactor" && (
              <TableSearchInput
                value={search}
                onChange={setSearch}
                placeholder="Search credentials…"
                className="w-72"
                maxLength={200}
              />
            )}
          </div>
        </div>

        <TabsContent value="passwords" className="space-y-4">
          <CredentialsList
            filter="password"
            search={debouncedSearch}
            folderId={selectedFolderId}
            isResolvingFolder={isResolvingFolder}
            onStartBackgroundTest={startBackgroundTest}
          />
        </TabsContent>

        <TabsContent value="creditCards" className="space-y-4">
          <CredentialsList
            filter="credit_card"
            search={debouncedSearch}
            folderId={selectedFolderId}
            isResolvingFolder={isResolvingFolder}
            onStartBackgroundTest={startBackgroundTest}
          />
        </TabsContent>

        <TabsContent value="secrets" className="space-y-4">
          <CredentialsList
            filter="secret"
            search={debouncedSearch}
            folderId={selectedFolderId}
            isResolvingFolder={isResolvingFolder}
            onStartBackgroundTest={startBackgroundTest}
          />
        </TabsContent>

        <TabsContent value="twoFactor" className="space-y-4">
          <CredentialsTotpTab />
        </TabsContent>
      </Tabs>
      <CredentialsModal onStartBackgroundTest={startBackgroundTest} />

      <CreateCredentialFolderDialog
        open={isCreateFolderOpen}
        onOpenChange={setIsCreateFolderOpen}
      />
      <ViewAllCredentialFoldersDialog
        open={isViewAllFoldersOpen}
        onOpenChange={setIsViewAllFoldersOpen}
        selectedFolderId={selectedFolderId}
        onFolderSelect={setSelectedFolderId}
      />

      {/* Footer note - only for Passwords and Credit Cards tabs */}
      {activeTab !== "twoFactor" && (
        <div className="mt-8 border-t border-slate-700 pt-4">
          <div className="text-sm italic text-neutral-600 dark:text-slate-400">
            <strong>Note:</strong> This feature requires a Bitwarden-compatible
            server ({" "}
            <a
              href="https://bitwarden.com/help/self-host-an-organization/"
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-400 underline hover:text-blue-300"
            >
              self-hosted Bitwarden
            </a>{" "}
            ) or{" "}
            <a
              href="https://github.com/dani-garcia/vaultwarden"
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-400 underline hover:text-blue-300"
            >
              this community version
            </a>{" "}
            or a paid Bitwarden account. Make sure the relevant
            `SKYVERN_AUTH_BITWARDEN_*` environment variables are configured. See
            details{" "}
            <a
              href="https://docs.skyvern.com/credentials/bitwarden"
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-400 underline hover:text-blue-300"
            >
              here
            </a>
            .
          </div>
        </div>
      )}
    </div>
  );
}

export { CredentialsPage };
