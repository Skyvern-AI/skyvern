import { LightningBoltIcon } from "@radix-ui/react-icons";

import { Tip } from "@/components/Tip";
import { Status, Task, WorkflowRunApiResponse } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { StatusFilterDropdown } from "@/components/StatusFilterDropdown";
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
import { useRunsQuery } from "@/hooks/useRunsQuery";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

function isTask(run: Task | WorkflowRunApiResponse): run is Task {
  return "task_id" in run;
}

function RunHistory() {
  const credentialGetter = useCredentialGetter();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const itemsPerPage = searchParams.get("page_size")
    ? Number(searchParams.get("page_size"))
    : 10;
  const [statusFilters, setStatusFilters] = useState<Array<Status>>([]);
  const { data: runs, isFetching } = useRunsQuery({
    page,
    pageSize: itemsPerPage,
    statusFilters,
  });
  const navigate = useNavigate();

  const { data: nextPageRuns } = useQuery<Array<Task | WorkflowRunApiResponse>>(
    {
      queryKey: ["runs", { statusFilters }, page + 1, itemsPerPage],
      queryFn: async () => {
        const client = await getClient(credentialGetter);
        const params = new URLSearchParams();
        params.append("page", String(page + 1));
        params.append("page_size", String(itemsPerPage));
        if (statusFilters) {
          statusFilters.forEach((status) => {
            params.append("status", status);
          });
        }
        return client.get("/runs", { params }).then((res) => res.data);
      },
      enabled: runs && runs.length === itemsPerPage,
    },
  );

  const isNextDisabled =
    isFetching || !nextPageRuns || nextPageRuns.length === 0;

  function handleNavigate(event: React.MouseEvent, path: string) {
    if (event.ctrlKey || event.metaKey) {
      window.open(
        window.location.origin + path,
        "_blank",
        "noopener,noreferrer",
      );
    } else {
      navigate(path);
    }
  }

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
  return (
    <div className="space-y-4">
      <div className="flex justify-between">
        <h1 className="text-2xl">Run History</h1>
        <StatusFilterDropdown
          values={statusFilters}
          onChange={setStatusFilters}
        />
      </div>
      <div className="rounded-lg border">
        <Table>
          <TableHeader className="rounded-t-lg bg-slate-elevation2">
            <TableRow>
              <TableHead className="w-1/4 rounded-tl-lg text-slate-400">
                Run ID
              </TableHead>
              <TableHead className="w-1/4 text-slate-400">Detail</TableHead>
              <TableHead className="w-1/4 text-slate-400">Status</TableHead>
              <TableHead className="w-1/4 rounded-tr-lg text-slate-400">
                Created At
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isFetching ? (
              Array.from({ length: 10 }).map((_, index) => (
                <TableRow key={index}>
                  <TableCell colSpan={4}>
                    <Skeleton className="h-4 w-full" />
                  </TableCell>
                </TableRow>
              ))
            ) : runs?.length === 0 ? (
              <TableRow>
                <TableCell colSpan={4}>
                  <div className="text-center">No runs found</div>
                </TableCell>
              </TableRow>
            ) : (
              runs?.map((run) => {
                if (isTask(run)) {
                  return (
                    <TableRow
                      key={run.task_id}
                      className="cursor-pointer"
                      onClick={(event) => {
                        handleNavigate(event, `/tasks/${run.task_id}/actions`);
                      }}
                    >
                      <TableCell className="max-w-0 truncate">
                        {run.task_id}
                      </TableCell>
                      <TableCell className="max-w-0 truncate">
                        {run.url}
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={run.status} />
                      </TableCell>
                      <TableCell
                        title={basicTimeFormat(run.created_at)}
                        className="max-w-0 truncate"
                      >
                        {basicLocalTimeFormat(run.created_at)}
                      </TableCell>
                    </TableRow>
                  );
                }

                const workflowTitle =
                  run.script_run === true ? (
                    <div className="flex items-center gap-2">
                      <Tip content="Ran with code">
                        <LightningBoltIcon className="text-[gold]" />
                      </Tip>
                      <span>{run.workflow_title ?? ""}</span>
                    </div>
                  ) : (
                    run.workflow_title ?? ""
                  );

                return (
                  <TableRow
                    key={run.workflow_run_id}
                    className="cursor-pointer"
                    onClick={(event) => {
                      handleNavigate(
                        event,
                        `/workflows/${run.workflow_permanent_id}/${run.workflow_run_id}/overview`,
                      );
                    }}
                  >
                    <TableCell
                      className="max-w-0 truncate"
                      title={run.workflow_run_id}
                    >
                      {run.workflow_run_id}
                    </TableCell>
                    <TableCell
                      className="max-w-0 truncate"
                      title={run.workflow_title ?? undefined}
                    >
                      {workflowTitle}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={run.status} />
                    </TableCell>
                    <TableCell
                      className="max-w-0 truncate"
                      title={basicTimeFormat(run.created_at)}
                    >
                      {basicLocalTimeFormat(run.created_at)}
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

export { RunHistory };
