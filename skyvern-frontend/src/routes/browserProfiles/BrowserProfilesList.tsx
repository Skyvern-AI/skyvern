import { ReloadIcon } from "@radix-ui/react-icons";
import { useSearchParams } from "react-router-dom";

import { BrowserIcon } from "@/components/icons/BrowserIcon";
import { Button } from "@/components/ui/button";
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
};

function BrowserProfilesList({ searchKey }: Props = {}) {
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
  });

  const { data: nextPageProfiles } = useBrowserProfilesQuery({
    page: page + 1,
    page_size: itemsPerPage,
    searchKey,
    enabled: (profiles?.length ?? 0) === itemsPerPage,
  });

  const isNextDisabled =
    isFetching || !nextPageProfiles || nextPageProfiles.length === 0;

  const activeCreate = useBrowserProfileCreateStore((state) => state.active);
  const hasSearch = Boolean(searchKey && searchKey.length > 0);
  const showPlaceholderRow = Boolean(activeCreate && page === 1 && !hasSearch);

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

  const pageItems = profiles ?? [];

  if (pageItems.length === 0 && page === 1 && !showPlaceholderRow) {
    return (
      <div className="rounded-md border border-slate-700 bg-slate-elevation1 p-10 text-sm text-neutral-600 dark:text-slate-300">
        {hasSearch ? (
          <>No browser profiles match &ldquo;{searchKey}&rdquo;.</>
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
            <TableRow>
              <TableHead className="w-[22%] truncate">Name</TableHead>
              <TableHead className="w-[40%] truncate">Description</TableHead>
              <TableHead className="w-[15%] truncate">Source Browser</TableHead>
              <TableHead className="w-[15%] truncate">Created</TableHead>
              <TableHead className="w-32 text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {showPlaceholderRow && activeCreate ? (
              <TableRow className="opacity-70">
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
            {pageItems.map((profile) => (
              <BrowserProfileItem
                key={profile.browser_profile_id}
                profile={profile}
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
    </div>
  );
}

export { BrowserProfilesList };
