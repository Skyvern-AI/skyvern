import { ReloadIcon } from "@radix-ui/react-icons";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { getClient } from "@/api/AxiosClient";
import { BrowserIcon } from "@/components/icons/BrowserIcon";
import { GarbageIcon } from "@/components/icons/GarbageIcon";
import { SelectionBar } from "@/components/SelectionBar";
import { SelectionHeaderCheckboxCell } from "@/components/SelectionCheckbox";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useRowSelection } from "@/hooks/useRowSelection";
import { bulkResultToast } from "@/util/bulkResultToast";
import {
  BULK_CONCURRENCY_LIMIT,
  runWithConcurrency,
} from "@/util/runWithConcurrency";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useBrowserProfilesQuery } from "@/routes/workflows/hooks/useBrowserProfilesQuery";
import { useBrowserProfileCreateStore } from "@/store/useBrowserProfileCreateStore";
import { cn } from "@/util/utils";

import { BrowserProfileItem } from "./BrowserProfileItem";

type Props = {
  searchKey?: string;
  managed?: boolean;
};

function BrowserProfilesList({ searchKey, managed }: Props = {}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const itemsPerPage = searchParams.get("page_size")
    ? Number(searchParams.get("page_size"))
    : 10;

  function setParamPatch(patch: Record<string, string>) {
    const params = new URLSearchParams(searchParams);
    Object.entries(patch).forEach(([k, v]) => params.set(k, v));
    setSearchParams(params, { replace: true });
  }

  function handlePreviousPage() {
    if (page === 1) return;
    setParamPatch({ page: String(page - 1) });
  }

  function handleNextPage() {
    if (isNextDisabled) return;
    setParamPatch({ page: String(page + 1) });
  }

  const {
    data: profiles,
    isLoading,
    isError,
    refetch,
    isFetching,
  } = useBrowserProfilesQuery({
    page,
    page_size: itemsPerPage,
    searchKey,
    managed,
  });

  const { data: nextPageProfiles } = useBrowserProfilesQuery({
    page: page + 1,
    page_size: itemsPerPage,
    searchKey,
    managed,
    enabled: (profiles?.length ?? 0) === itemsPerPage,
  });

  const isNextDisabled =
    isFetching || !nextPageProfiles || nextPageProfiles.length === 0;

  const activeCreate = useBrowserProfileCreateStore((state) => state.active);
  const hasSearch = Boolean(searchKey && searchKey.length > 0);
  const showPlaceholderRow = Boolean(activeCreate && page === 1 && !hasSearch);

  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [isBulkOperating, setIsBulkOperating] = useState(false);
  const [deleteDialog, setDeleteDialog] = useState<{
    open: boolean;
    targets: string[];
  }>({ open: false, targets: [] });

  // Selection indices must address the same array the table renders; keep one source for both.
  const pageItems = profiles ?? [];

  const {
    selected,
    selectedItems: selectedProfiles,
    isSelected,
    allSelected,
    someSelected,
    handleSelect,
    toggleSelectAll,
    clearSelection,
    replaceSelection,
  } = useRowSelection({
    items: pageItems,
    getId: (profile) => profile.browser_profile_id,
    resetKey: JSON.stringify([page, itemsPerPage, searchKey ?? "", managed]),
  });

  async function handleBulkDeleteConfirm() {
    const targets = deleteDialog.targets;
    if (targets.length === 0) {
      return;
    }
    setIsBulkOperating(true);
    try {
      // Browser-profile endpoints live on /v1 (no /api prefix).
      const client = await getClient(credentialGetter, "sans-api-v1");
      const results = await runWithConcurrency(
        targets.map(
          (profileId) => () => client.delete(`/browser_profiles/${profileId}`),
        ),
        BULK_CONCURRENCY_LIMIT,
      );
      const failedIds = new Set<string>();
      const succeededIds: string[] = [];
      results.forEach((result, index) => {
        if (result.status === "fulfilled") {
          succeededIds.push(targets[index]!);
        } else {
          failedIds.add(targets[index]!);
        }
      });
      bulkResultToast({
        succeeded: succeededIds.length,
        total: targets.length,
        results,
        successTitle: (n) => `Deleted ${n} profile${n !== 1 ? "s" : ""}.`,
        failureTitle: (n) =>
          `Failed to delete ${n} profile${n !== 1 ? "s" : ""}.`,
        partialTitle: (successCount, failedCount) =>
          `Deleted ${successCount} profile${successCount !== 1 ? "s" : ""}. ${failedCount} failed.`,
      });
      if (failedIds.size === 0) {
        clearSelection();
      } else {
        replaceSelection(failedIds);
      }
      if (succeededIds.length > 0) {
        queryClient.invalidateQueries({ queryKey: ["browserProfiles"] });
        succeededIds.forEach((profileId) => {
          queryClient.invalidateQueries({
            queryKey: ["browserProfile", profileId],
          });
        });
      }
    } finally {
      setIsBulkOperating(false);
      setDeleteDialog({ open: false, targets: [] });
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="rounded-md border border-slate-700 bg-slate-elevation1 p-6 text-sm text-neutral-600 dark:text-slate-300">
        <div className="mb-3">Failed to load browser profiles.</div>
        <Button
          variant="secondary"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          {isFetching && <ReloadIcon className="mr-2 size-4 animate-spin" />}
          Retry
        </Button>
      </div>
    );
  }

  if (pageItems.length === 0 && page === 1 && !showPlaceholderRow) {
    return (
      <div className="rounded-md border border-slate-700 bg-slate-elevation1 p-10 text-sm text-neutral-600 dark:text-slate-300">
        {hasSearch ? (
          <>No browser profiles match &ldquo;{searchKey}&rdquo;.</>
        ) : managed === true ? (
          <div className="flex flex-col items-center gap-3 text-center">
            <BrowserIcon className="size-10 text-neutral-600 dark:text-slate-400" />
            <div className="space-y-1">
              <p className="text-base font-medium text-slate-100">
                No auto-managed profiles yet
              </p>
              <p className="mx-auto max-w-md text-sm text-neutral-600 dark:text-slate-400">
                Run a workflow with Save &amp; Reuse Session enabled and Skyvern
                creates one automatically.
              </p>
            </div>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-3 text-center">
            <BrowserIcon className="size-10 text-neutral-600 dark:text-slate-400" />
            <div className="space-y-1">
              <p className="text-base font-medium text-slate-100">
                No browser profiles yet
              </p>
              <p className="mx-auto max-w-md text-sm text-neutral-600 dark:text-slate-400">
                Start a session, then click Save Profile on the session page to
                capture it here.
              </p>
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="overflow-hidden rounded-lg border border-border">
        <Table className="w-full table-fixed">
          <TableHeader>
            <TableRow className="group/header">
              <SelectionHeaderCheckboxCell
                className="w-10"
                allSelected={allSelected}
                someSelected={someSelected}
                hasSelection={selected.size > 0}
                onToggleAll={toggleSelectAll}
                ariaLabel="Select all browser profiles"
              />
              <TableHead className="w-[20%] truncate">Name</TableHead>
              <TableHead className="w-[37%] truncate">Description</TableHead>
              <TableHead className="w-[15%] truncate">Source Browser</TableHead>
              <TableHead className="w-[15%] truncate">Created</TableHead>
              <TableHead className="w-32 text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {showPlaceholderRow && activeCreate ? (
              <TableRow className="opacity-70">
                <TableCell />
                <TableCell className="truncate">
                  <div className="flex min-w-0 items-center gap-2">
                    <ReloadIcon className="h-4 w-4 shrink-0 animate-spin text-blue-400" />
                    <span className="truncate" title={activeCreate.name}>
                      {activeCreate.name}
                    </span>
                  </div>
                </TableCell>
                <TableCell className="text-slate-400">
                  <span className="opacity-50">—</span>
                </TableCell>
                <TableCell className="text-slate-400">
                  <span className="opacity-50">—</span>
                </TableCell>
                <TableCell className="text-slate-400">
                  <span className="opacity-50">—</span>
                </TableCell>
                <TableCell>
                  <span className="opacity-50">—</span>
                </TableCell>
              </TableRow>
            ) : null}
            {pageItems.map((profile, index) => (
              <BrowserProfileItem
                key={profile.browser_profile_id}
                profile={profile}
                index={index}
                selected={isSelected(profile.browser_profile_id)}
                hasSelection={selected.size > 0}
                onSelect={handleSelect}
              />
            ))}
          </TableBody>
        </Table>
        <div className="relative px-3 py-3">
          <div className="absolute left-3 top-1/2 flex -translate-y-1/2 items-center gap-2 text-sm">
            <span className="text-neutral-600 dark:text-slate-400">
              Items per page
            </span>
            <select
              className="h-8 rounded-md border border-input bg-background px-2 text-sm"
              value={itemsPerPage}
              onChange={(e) => {
                const next = Number(e.target.value);
                const params = new URLSearchParams(searchParams);
                params.set("page_size", String(next));
                params.set("page", "1");
                setSearchParams(params, { replace: true });
              }}
            >
              <option value={5}>5</option>
              <option value={10}>10</option>
              <option value={20}>20</option>
              <option value={50}>50</option>
            </select>
          </div>
          <Pagination className="pt-0">
            <PaginationContent>
              <PaginationItem>
                <PaginationPrevious
                  className={cn({
                    "cursor-not-allowed opacity-50": page === 1,
                  })}
                  onClick={handlePreviousPage}
                />
              </PaginationItem>
              <PaginationItem>
                <PaginationLink>{page}</PaginationLink>
              </PaginationItem>
              <PaginationItem>
                <PaginationNext
                  className={cn({
                    "cursor-not-allowed opacity-50": isNextDisabled,
                  })}
                  onClick={handleNextPage}
                />
              </PaginationItem>
            </PaginationContent>
          </Pagination>
        </div>
      </div>
      {selectedProfiles.length > 0 && (
        <SelectionBar
          count={selectedProfiles.length}
          isOperating={isBulkOperating}
          onClear={clearSelection}
        >
          <Button
            size="sm"
            variant="ghost"
            className="text-destructive hover:bg-destructive/10 hover:text-destructive"
            disabled={isBulkOperating}
            onClick={() =>
              setDeleteDialog({
                open: true,
                targets: selectedProfiles.map(
                  (profile) => profile.browser_profile_id,
                ),
              })
            }
          >
            <GarbageIcon className="mr-1.5 h-4 w-4" />
            Delete
          </Button>
        </SelectionBar>
      )}
      <Dialog
        open={deleteDialog.open}
        onOpenChange={(open) => {
          if (!open && !isBulkOperating) {
            setDeleteDialog({ open: false, targets: [] });
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Delete {deleteDialog.targets.length} Browser Profile
              {deleteDialog.targets.length === 1 ? "" : "s"}
            </DialogTitle>
            <DialogDescription>
              Are you sure you want to delete {deleteDialog.targets.length}{" "}
              {deleteDialog.targets.length === 1
                ? "browser profile"
                : "browser profiles"}
              ? Linked credentials are unlinked automatically; any workflows
              pinned to these profiles will need repointing. This action cannot
              be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="secondary"
              disabled={isBulkOperating}
              onClick={() => setDeleteDialog({ open: false, targets: [] })}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={isBulkOperating}
              onClick={() => {
                void handleBulkDeleteConfirm();
              }}
            >
              {isBulkOperating ? "Deleting..." : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export { BrowserProfilesList };
