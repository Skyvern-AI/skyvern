import { LightningBoltIcon, MixerHorizontalIcon } from "@radix-ui/react-icons";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tip } from "@/components/Tip";
import {
  Status,
  TaskRunListItem,
  TaskRunType,
  WorkflowRunStatusApiResponse,
} from "@/api/types";
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
import {
  basicLocalTimeFormat,
  basicTimeFormat,
  formatExecutionTime,
} from "@/util/timeFormat";
import { cn } from "@/util/utils";
import { useQuery } from "@tanstack/react-query";
import React, { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useDebounce } from "use-debounce";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useGlobalWorkflowsQuery } from "@/routes/workflows/hooks/useGlobalWorkflowsQuery";
import { TableSearchInput } from "@/components/TableSearchInput";
import { useKeywordSearch } from "@/routes/workflows/hooks/useKeywordSearch";
import { useParameterExpansion } from "@/routes/workflows/hooks/useParameterExpansion";
import { ParameterDisplayInline } from "@/routes/workflows/components/ParameterDisplayInline";
import { HighlightText } from "@/routes/workflows/components/HighlightText";

const statusValues = new Set<string>(Object.values(Status));
function isKnownStatus(value: string): value is Status {
  return statusValues.has(value);
}

function getRunNavigationPath(run: TaskRunListItem): string {
  switch (run.task_run_type) {
    case TaskRunType.WorkflowRun:
    case TaskRunType.TaskV2:
      return `/runs/${run.run_id}`;
    case TaskRunType.TaskV1:
    case TaskRunType.OpenaiCua:
    case TaskRunType.AnthropicCua:
    case TaskRunType.UiTars:
      return `/tasks/${run.run_id}/actions`;
    default:
      return `/runs/${run.run_id}`;
  }
}

function RunHistory() {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const itemsPerPage = searchParams.get("page_size")
    ? Number(searchParams.get("page_size"))
    : 10;
  const [statusFilters, setStatusFilters] = useState<Array<Status>>([]);
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 500);

  const { data: runs, isFetching } = useRunsQuery({
    page,
    pageSize: itemsPerPage,
    statusFilters,
    search: debouncedSearch,
  });
  const navigate = useNavigate();

  const { data: nextPageRuns } = useRunsQuery({
    page: page + 1,
    pageSize: itemsPerPage,
    statusFilters,
    search: debouncedSearch,
    enabled: runs?.length === itemsPerPage,
  });

  const isNextDisabled =
    isFetching || !nextPageRuns || nextPageRuns.length === 0;

  const { matchesParameter, isSearchActive } =
    useKeywordSearch(debouncedSearch);
  const {
    expandedRows,
    toggleExpanded: toggleParametersExpanded,
    setAutoExpandedRows,
  } = useParameterExpansion();

  useEffect(() => {
    if (!isSearchActive) {
      setAutoExpandedRows([]);
      return;
    }

    const workflowRunIds =
      runs
        ?.filter((run) => run.task_run_type === TaskRunType.WorkflowRun)
        .map((run) => run.run_id)
        .filter((id): id is string => Boolean(id)) ?? [];

    setAutoExpandedRows(workflowRunIds);
  }, [isSearchActive, runs, setAutoExpandedRows]);

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

  const displayTableBody = () => {
    // Show loading skeleton
    if (isFetching) {
      return Array.from({ length: 10 }).map((_, index) => (
        <TableRow key={`row-${index}`}>
          <TableCell colSpan={6}>
            <Skeleton className="h-4 w-full" />
          </TableCell>
        </TableRow>
      ));
    }

    // No runs found
    if (runs?.length === 0) {
      return (
        <TableRow>
          <TableCell colSpan={6}>
            <div className="text-center">No runs found</div>
          </TableCell>
        </TableRow>
      );
    }

    return runs?.map((run) => {
      const executionTime = formatExecutionTime(
        run.started_at ?? run.created_at,
        run.finished_at,
      );
      const isWorkflowRun = run.task_run_type === TaskRunType.WorkflowRun;
      const isExpanded = isWorkflowRun && expandedRows.has(run.run_id);
      const navPath = getRunNavigationPath(run);

      const titleContent = run.script_run ? (
        <div className="flex items-center gap-2">
          <Tip content="Ran with code">
            <LightningBoltIcon className="text-[gold]" />
          </Tip>
          <span>{run.title ?? ""}</span>
        </div>
      ) : (
        (run.title ?? "")
      );

      return (
        <React.Fragment key={run.task_run_id}>
          <TableRow
            className="cursor-pointer"
            onClick={(event) => {
              handleNavigate(event, navPath);
            }}
          >
            <TableCell className="max-w-0 truncate" title={run.run_id}>
              <HighlightText text={run.run_id} query={debouncedSearch} />
            </TableCell>
            <TableCell
              className="max-w-0 truncate"
              title={run.title ?? undefined}
            >
              {titleContent}
            </TableCell>
            <TableCell>
              {isKnownStatus(run.status) ? (
                <StatusBadge status={run.status} />
              ) : (
                <span className="text-sm text-slate-400">{run.status}</span>
              )}
            </TableCell>
            <TableCell
              className="max-w-0 truncate"
              title={basicTimeFormat(run.created_at)}
            >
              {basicLocalTimeFormat(run.created_at)}
            </TableCell>
            <TableCell className="text-slate-400">
              {executionTime ?? "-"}
            </TableCell>
            <TableCell>
              {isWorkflowRun ? (
                <div className="flex justify-end">
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          size="icon"
                          variant="outline"
                          onClick={(event) => {
                            event.stopPropagation();
                            toggleParametersExpanded(run.run_id);
                          }}
                          className={cn(isExpanded && "text-blue-400")}
                        >
                          <MixerHorizontalIcon className="h-4 w-4" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>
                        {isExpanded ? "Hide Parameters" : "Show Parameters"}
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
              ) : null}
            </TableCell>
          </TableRow>

          {isExpanded && run.workflow_permanent_id && (
            <TableRow key={`${run.run_id}-params`}>
              <TableCell
                colSpan={6}
                className="bg-slate-50 dark:bg-slate-900/50"
              >
                <WorkflowRunParametersInline
                  workflowPermanentId={run.workflow_permanent_id}
                  workflowRunId={run.run_id}
                  searchQuery={debouncedSearch}
                  keywordMatchesParameter={matchesParameter}
                />
              </TableCell>
            </TableRow>
          )}
        </React.Fragment>
      );
    });
  };

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl">Run History</h1>
      </header>
      <div className="flex items-center justify-between gap-4">
        <TableSearchInput
          value={search}
          onChange={(value) => {
            setSearch(value);
            const params = new URLSearchParams(searchParams);
            params.set("page", "1");
            setSearchParams(params, { replace: true });
          }}
          placeholder="Search by run ID or parameter..."
          className="w-48 lg:w-72"
        />
        <StatusFilterDropdown
          values={statusFilters}
          onChange={(filters) => {
            setStatusFilters(filters);
            const params = new URLSearchParams(searchParams);
            params.set("page", "1");
            setSearchParams(params, { replace: true });
          }}
        />
      </div>
      <div className="rounded-lg border">
        <Table className="sm:table-fixed">
          <TableHeader className="rounded-t-lg bg-slate-elevation2">
            <TableRow>
              <TableHead className="w-[20%] rounded-tl-lg text-slate-400">
                Run ID
              </TableHead>
              <TableHead className="w-[20%] text-slate-400">Detail</TableHead>
              <TableHead className="w-[16%] text-slate-400">Status</TableHead>
              <TableHead className="w-[27%] text-slate-400">
                Created At
              </TableHead>
              <TableHead className="w-[8%] text-slate-400">Duration</TableHead>
              <TableHead className="w-[8%] rounded-tr-lg text-slate-400"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>{displayTableBody()}</TableBody>
        </Table>
        <div className="flex items-center justify-between px-3 py-3">
          <div className="flex items-center gap-2">
            <span className="text-sm text-slate-400">Items per page</span>
            <Select
              value={String(itemsPerPage)}
              onValueChange={(size) => {
                const params = new URLSearchParams(searchParams);
                params.set("page_size", size);
                params.set("page", "1");
                setSearchParams(params, { replace: true });
              }}
            >
              <SelectTrigger className="w-[65px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="5">5</SelectItem>
                <SelectItem value="10">10</SelectItem>
                <SelectItem value="20">20</SelectItem>
                <SelectItem value="50">50</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Pagination className="mx-0 w-auto pt-0">
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

type WorkflowRunParametersInlineProps = {
  workflowPermanentId: string;
  workflowRunId: string;
  searchQuery: string;
  keywordMatchesParameter: (parameter: {
    key: string;
    value: unknown;
    description?: string | null;
  }) => boolean;
};

function WorkflowRunParametersInline({
  workflowPermanentId,
  workflowRunId,
  searchQuery,
  keywordMatchesParameter,
}: Readonly<WorkflowRunParametersInlineProps>) {
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const credentialGetter = useCredentialGetter();

  const { data: run, isLoading } = useQuery<WorkflowRunStatusApiResponse>({
    queryKey: [
      "workflowRun",
      workflowPermanentId,
      workflowRunId,
      "params-inline",
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      const isGlobalWorkflow = globalWorkflows?.some(
        (workflow) => workflow.workflow_permanent_id === workflowPermanentId,
      );
      if (isGlobalWorkflow) {
        params.set("template", "true");
      }
      return client
        .get(`/workflows/${workflowPermanentId}/runs/${workflowRunId}`, {
          params,
        })
        .then((r) => r.data);
    },
    enabled: !!workflowPermanentId && !!workflowRunId && !!globalWorkflows,
  });

  if (isLoading) {
    return (
      <div className="ml-8 py-4">
        <Skeleton className="h-20 w-full" />
      </div>
    );
  }

  const hasParameters =
    run?.parameters && Object.keys(run.parameters).length > 0;
  const hasExtraHeaders =
    run?.extra_http_headers && Object.keys(run.extra_http_headers).length > 0;

  if (!hasParameters && !hasExtraHeaders) {
    return (
      <div className="ml-8 py-4 text-sm text-slate-400">
        No parameters for this run
      </div>
    );
  }

  const parameterItems = hasParameters
    ? Object.entries(run.parameters).map(([key, value]) => ({
        key,
        value,
        description: null,
      }))
    : [];

  const headerItems =
    hasExtraHeaders && run.extra_http_headers
      ? Object.entries(run.extra_http_headers).map(([key, value]) => ({
          key,
          value,
          description: null,
        }))
      : [];

  return (
    <div className="space-y-4">
      {hasParameters && (
        <ParameterDisplayInline
          title="Run Parameters"
          parameters={parameterItems}
          searchQuery={searchQuery}
          keywordMatchesParameter={keywordMatchesParameter}
          showDescription={false}
        />
      )}
      {hasExtraHeaders && (
        <ParameterDisplayInline
          title="Extra HTTP Headers"
          parameters={headerItems}
          searchQuery={searchQuery}
          keywordMatchesParameter={keywordMatchesParameter}
          showDescription={false}
        />
      )}
    </div>
  );
}

export { RunHistory };
