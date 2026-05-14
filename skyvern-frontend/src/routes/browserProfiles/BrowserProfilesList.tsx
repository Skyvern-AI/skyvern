import { ReloadIcon } from "@radix-ui/react-icons";
import { useSearchParams } from "react-router-dom";

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
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useBrowserProfilesQuery } from "@/routes/workflows/hooks/useBrowserProfilesQuery";
import { cn } from "@/util/utils";

import { BrowserProfileItem } from "./BrowserProfileItem";

function BrowserProfilesList() {
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
  });

  const { data: nextPageProfiles } = useBrowserProfilesQuery({
    page: page + 1,
    page_size: itemsPerPage,
    enabled: (profiles?.length ?? 0) === itemsPerPage,
  });

  const isNextDisabled =
    isFetching || !nextPageProfiles || nextPageProfiles.length === 0;

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
      <div className="rounded-md border border-slate-700 bg-slate-elevation1 p-6 text-sm text-slate-300">
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

  if (pageItems.length === 0 && page === 1) {
    return (
      <div className="rounded-md border border-slate-700 bg-slate-elevation1 p-6 text-sm text-slate-300">
        No browser profiles yet. Profiles are created via the API/SDK, or when a
        credential test or workflow run saves its persistent browser session.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border">
        <Table className="w-full table-fixed">
          <TableHeader className="rounded-t-lg bg-slate-elevation2">
            <TableRow>
              <TableHead className="w-1/4 truncate rounded-tl-lg text-slate-400">
                Name
              </TableHead>
              <TableHead className="w-1/3 truncate text-slate-400">
                Description
              </TableHead>
              <TableHead className="w-1/6 truncate text-slate-400">
                Source Browser
              </TableHead>
              <TableHead className="w-1/6 truncate text-slate-400">
                Created
              </TableHead>
              <TableHead className="w-32 truncate rounded-tr-lg text-right text-slate-400">
                Actions
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
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
            <span className="text-slate-400">Items per page</span>
            <select
              className="h-9 rounded-md border border-slate-300 bg-background px-3"
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
