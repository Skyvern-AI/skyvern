import {
  ExternalLinkIcon,
  GlobeIcon,
  PlusIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useSearchParams } from "react-router-dom";

import { HelpTooltip } from "@/components/HelpTooltip";
import { Button } from "@/components/ui/button";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useBrowserSessionsQuery } from "@/routes/browserSessions/hooks/useBrowserSessionsQuery";
import { useCreateBrowserSessionMutation } from "@/routes/browserSessions/hooks/useCreateBrowserSessionMutation";
import { type BrowserSession } from "@/routes/workflows/types/browserSessionTypes";
import { CopyText } from "@/routes/workflows/editor/Workspace";
import { basicTimeFormat } from "@/util/timeFormat";
import { cn, formatMs } from "@/util/utils";

function toDate(
  time: string,
  defaultDate: Date | null = new Date(0),
): Date | null {
  time = time.replace(/\.(\d{3})\d*/, ".$1");

  if (!time.endsWith("Z")) {
    time += "Z";
  }

  const date = new Date(time);

  if (isNaN(date.getTime())) {
    return defaultDate;
  }

  return date;
}

function sessionIsOpen(browserSession: BrowserSession): boolean {
  return (
    browserSession.completed_at === null && browserSession.started_at !== null
  );
}

function BrowserSessions() {
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

  const createBrowserSessionMutation = useCreateBrowserSessionMutation();

  const { data: browserSessions = [], isLoading } = useBrowserSessionsQuery(
    page,
    itemsPerPage,
  );

  const { data: nextPageBrowserSessions } = useBrowserSessionsQuery(
    page + 1,
    itemsPerPage,
  );

  const isNextDisabled =
    isLoading ||
    !nextPageBrowserSessions ||
    nextPageBrowserSessions.length === 0;

  function handleRowClick(browserSessionId: string) {
    window.open(
      window.location.origin + `/browser-session/${browserSessionId}`,
      "_blank",
      "noopener,noreferrer",
    );
  }

  return (
    <div className="px-8">
      {/* header */}
      <div className="space-y-5">
        <div className="flex items-center gap-2">
          <GlobeIcon className="size-6" />
          <h1 className="text-2xl">Browsers</h1>
        </div>
        <p className="text-slate-300">
          Create your own live browsers to interact with websites, or run
          workflows in.
        </p>
      </div>

      {/* browsers */}
      <div className="mt-6 space-y-4">
        <div className="flex justify-end">
          <div className="flex gap-4">
            <Button
              disabled={createBrowserSessionMutation.isPending}
              onClick={() => {
                createBrowserSessionMutation.mutate({
                  proxyLocation: null,
                  timeout: null,
                });
              }}
            >
              {createBrowserSessionMutation.isPending ? (
                <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <PlusIcon className="mr-2 h-4 w-4" />
              )}
              Create
            </Button>
          </div>
        </div>
        <div className="rounded-lg border">
          <Table className="w-full table-fixed">
            <TableHeader className="rounded-t-lg bg-slate-elevation2">
              <TableRow>
                <TableHead className="w-1/4 truncate rounded-tl-lg text-slate-400">
                  ID
                </TableHead>
                <TableHead className="w-1/12 truncate text-slate-400">
                  Open
                </TableHead>
                <TableHead className="w-1/6 truncate text-slate-400">
                  <span className="mr-2">Occupied</span>
                  <HelpTooltip
                    className="inline"
                    content="Browser is busy running a task or workflow"
                  />
                </TableHead>
                <TableHead className="w-1/6 truncate text-slate-400">
                  Started
                </TableHead>
                <TableHead className="w-1/6 truncate text-slate-400">
                  Timeout
                </TableHead>
                <TableHead className="w-1/2 truncate text-slate-400">
                  CDP Url
                </TableHead>
                <TableHead className="w-[2.5rem] rounded-tr-lg"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading ? (
                <TableRow>
                  <TableCell colSpan={6}>Loading...</TableCell>
                </TableRow>
              ) : browserSessions?.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6}>No browser sessions found</TableCell>
                </TableRow>
              ) : (
                browserSessions?.map((browserSession) => {
                  const isOpen = sessionIsOpen(browserSession);
                  const startedAtDate = toDate(
                    browserSession.started_at ?? "",
                    null,
                  );
                  const ago = startedAtDate ? (
                    formatMs(Date.now() - startedAtDate.getTime()).ago
                  ) : (
                    <span className="opacity-50">never</span>
                  );
                  const cdpUrl =
                    browserSession.browser_address ??
                    "wss://session-staging.skyvern.com/pbs_442960015326262218/devtools/browser/f01f27e1-182b-4a33-9017-4c1146d3eb3e";

                  return (
                    <TableRow key={browserSession.browser_session_id}>
                      <TableCell>
                        <div className="flex items-center">
                          <div className="flex-1 truncate opacity-75">
                            {browserSession.browser_session_id}
                          </div>
                          <CopyText
                            className="opacity-75 hover:opacity-100"
                            text={browserSession.browser_session_id}
                          />
                        </div>
                      </TableCell>
                      <TableCell>
                        {isOpen ? (
                          "Yes"
                        ) : (
                          <span className="opacity-50">No</span>
                        )}
                      </TableCell>
                      <TableCell>
                        {browserSession.runnable_id ? (
                          "Yes"
                        ) : (
                          <span className="opacity-50">No</span>
                        )}
                      </TableCell>
                      <TableCell
                        title={
                          browserSession.started_at
                            ? basicTimeFormat(browserSession.started_at)
                            : "not started"
                        }
                      >
                        {ago}
                      </TableCell>
                      <TableCell>
                        {browserSession.timeout
                          ? `${browserSession.timeout}m`
                          : "-"}
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center">
                          <div className="flex-1 truncate opacity-75">
                            {cdpUrl}
                          </div>
                          {cdpUrl !== "-" ? (
                            <CopyText
                              className="opacity-75 hover:opacity-100"
                              text={cdpUrl}
                            />
                          ) : null}
                        </div>
                      </TableCell>
                      <TableCell
                        className="cursor-pointer"
                        onClick={() => {
                          handleRowClick(browserSession.browser_session_id);
                        }}
                      >
                        <ExternalLinkIcon className="inline size-4" />
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>
          <div className="relative px-3 py-3">
            <div className="absolute left-3 top-1/2 flex -translate-y-1/2 items-center gap-2 text-sm">
              <span className="text-slate-400">Items per page</span>
              <select
                className="h-9 rounded-md border border-slate-300 bg-background"
                value={itemsPerPage}
                onChange={(e) => {
                  const next = Number(e.target.value);
                  const params = new URLSearchParams(searchParams);
                  params.set("page_size", String(next));
                  params.set("page", "1");
                  setSearchParams(params, { replace: true });
                }}
              >
                <option className="px-3" value={5}>
                  5
                </option>
                <option className="px-3" value={10}>
                  10
                </option>
                <option className="px-3" value={20}>
                  20
                </option>
                <option className="px-3" value={50}>
                  50
                </option>
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
    </div>
  );
}

export { BrowserSessions };
