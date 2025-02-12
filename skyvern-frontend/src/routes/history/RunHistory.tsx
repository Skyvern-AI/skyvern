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
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

function isTask(run: Task | WorkflowRunApiResponse): run is Task {
  return "task_id" in run;
}

function RunHistory() {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const [statusFilters, setStatusFilters] = useState<Array<Status>>([]);
  const { data: runs, isFetching } = useRunsQuery({ page, statusFilters });
  const navigate = useNavigate();

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
                      {run.workflow_title ?? ""}
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
        <Pagination className="pt-2">
          <PaginationContent>
            <PaginationItem>
              <PaginationPrevious
                className={cn({ "cursor-not-allowed": page === 1 })}
                onClick={() => {
                  if (page === 1) {
                    return;
                  }
                  const params = new URLSearchParams();
                  params.set("page", String(Math.max(1, page - 1)));
                  setSearchParams(params, { replace: true });
                }}
              />
            </PaginationItem>
            <PaginationItem>
              <PaginationLink>{page}</PaginationLink>
            </PaginationItem>
            <PaginationItem>
              <PaginationNext
                onClick={() => {
                  const params = new URLSearchParams();
                  params.set("page", String(page + 1));
                  setSearchParams(params, { replace: true });
                }}
              />
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      </div>
    </div>
  );
}

export { RunHistory };
