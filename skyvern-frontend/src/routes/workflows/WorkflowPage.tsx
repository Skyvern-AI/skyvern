import { LightningBoltIcon } from "@radix-ui/react-icons";

import { Tip } from "@/components/Tip";
import { Status } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { StatusFilterDropdown } from "@/components/StatusFilterDropdown";
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
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import {
  MixerHorizontalIcon,
  Pencil2Icon,
  PlayIcon,
} from "@radix-ui/react-icons";
import React, { useEffect, useState } from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useWorkflowQuery } from "./hooks/useWorkflowQuery";
import { useWorkflowRunsQuery } from "./hooks/useWorkflowRunsQuery";
import { WorkflowActions } from "./WorkflowActions";
import { useDebounce } from "use-debounce";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { RunParametersDialog } from "./workflowRun/RunParametersDialog";
import * as env from "@/util/env";
import { getClient } from "@/api/AxiosClient";
import { WorkflowRunStatusApiResponse } from "@/api/types";
import { useQuery } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useGlobalWorkflowsQuery } from "./hooks/useGlobalWorkflowsQuery";
import { TableSearchInput } from "@/components/TableSearchInput";
import { useKeywordSearch } from "./hooks/useKeywordSearch";
import { useParameterExpansion } from "./hooks/useParameterExpansion";
import { ParameterDisplayInline } from "./components/ParameterDisplayInline";

function WorkflowPage() {
  const { workflowPermanentId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const [statusFilters, setStatusFilters] = useState<Array<Status>>([]);
  const navigate = useNavigate();

  const PAGE_SIZE = 10;
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 500);
  const [openRunParams, setOpenRunParams] = useState<string | null>(null);
  const { matchesParameter, isSearchActive } =
    useKeywordSearch(debouncedSearch);
  const {
    expandedRows,
    toggleExpanded: toggleParametersExpanded,
    setAutoExpandedRows,
  } = useParameterExpansion();

  const { data: workflowRuns, isLoading } = useWorkflowRunsQuery({
    workflowPermanentId,
    statusFilters,
    page,
    search: debouncedSearch,
    refetchOnMount: "always",
  });

  useEffect(() => {
    if (!isLoading && workflowRuns && workflowRuns.length === 0 && page > 1) {
      const params = new URLSearchParams();
      params.set("page", String(page - 1));
      setSearchParams(params, { replace: true });
    }
  }, [workflowRuns, isLoading, page, setSearchParams]);

  const { data: workflow, isLoading: workflowIsLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  useEffect(() => {
    if (!isSearchActive) {
      setAutoExpandedRows([]);
      return;
    }

    const runIds =
      workflowRuns
        ?.map((run) => run.workflow_run_id)
        .filter((id): id is string => Boolean(id)) ?? [];

    setAutoExpandedRows(runIds);
  }, [isSearchActive, workflowRuns, setAutoExpandedRows]);

  if (!workflowPermanentId) {
    return null; // this should never happen
  }

  return (
    <div className="space-y-8">
      <header className="flex justify-between">
        <div className="flex flex-col gap-2">
          {workflowIsLoading ? (
            <>
              <Skeleton className="h-7 w-56" />
              <Skeleton className="h-7 w-56" />
            </>
          ) : (
            <>
              <h1 className="text-lg font-semibold">{workflow?.title}</h1>
              <h2 className="text-sm">{workflowPermanentId}</h2>
            </>
          )}
        </div>
        <div className="flex gap-2">
          {workflow && (
            <WorkflowActions
              workflow={workflow}
              onSuccessfullyDeleted={() => navigate("/workflows")}
            />
          )}
          <Button asChild variant="secondary">
            <Link to={`/workflows/${workflowPermanentId}/debug`}>
              <Pencil2Icon className="mr-2 size-4" />
              Edit
            </Link>
          </Button>
          <Button asChild>
            <Link to={`/workflows/${workflowPermanentId}/run`}>
              <PlayIcon className="mr-2 size-4" />
              Run
            </Link>
          </Button>
        </div>
      </header>
      <div className="space-y-4">
        <header>
          <h1 className="text-2xl">Past Runs</h1>
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
            placeholder="Search runs by parameter..."
            className="w-48 lg:w-72"
          />
          <StatusFilterDropdown
            values={statusFilters}
            onChange={setStatusFilters}
          />
        </div>
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-1/4">ID</TableHead>
                <TableHead className="w-1/4">Status</TableHead>
                <TableHead className="w-1/4">Created At</TableHead>
                <TableHead className="w-1/4"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading ? (
                <TableRow>
                  <TableCell colSpan={4}>Loading...</TableCell>
                </TableRow>
              ) : workflowRuns?.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={4}>No workflow runs found</TableCell>
                </TableRow>
              ) : (
                workflowRuns?.map((workflowRun) => {
                  const workflowRunId =
                    workflowRun.script_run === true ? (
                      <div className="flex items-center gap-2">
                        <Tip content="Ran with code">
                          <LightningBoltIcon className="text-[gold]" />
                        </Tip>
                        <span>{workflowRun.workflow_run_id ?? ""}</span>
                      </div>
                    ) : (
                      workflowRun.workflow_run_id ?? ""
                    );

                  const isExpanded = expandedRows.has(
                    workflowRun.workflow_run_id,
                  );

                  return (
                    <React.Fragment key={workflowRun.workflow_run_id}>
                      {/* Main run row */}
                      <TableRow
                        onClick={(event) => {
                          const url = env.useNewRunsUrl
                            ? `/runs/${workflowRun.workflow_run_id}`
                            : `/workflows/${workflowPermanentId}/${workflowRun.workflow_run_id}/overview`;

                          if (event.ctrlKey || event.metaKey) {
                            window.open(
                              window.location.origin + url,
                              "_blank",
                              "noopener,noreferrer",
                            );
                            return;
                          }
                          navigate(url);
                        }}
                        className="cursor-pointer"
                      >
                        <TableCell>{workflowRunId}</TableCell>
                        <TableCell>
                          <StatusBadge status={workflowRun.status} />
                        </TableCell>
                        <TableCell
                          title={basicTimeFormat(workflowRun.created_at)}
                        >
                          {basicLocalTimeFormat(workflowRun.created_at)}
                        </TableCell>
                        <TableCell>
                          <div className="flex justify-end gap-2">
                            <TooltipProvider>
                              <Tooltip>
                                <TooltipTrigger asChild>
                                  <Button
                                    size="icon"
                                    variant="outline"
                                    onClick={(event) => {
                                      event.stopPropagation();
                                      toggleParametersExpanded(
                                        workflowRun.workflow_run_id,
                                      );
                                    }}
                                    className={cn(
                                      isExpanded && "text-blue-400",
                                    )}
                                  >
                                    <MixerHorizontalIcon className="h-4 w-4" />
                                  </Button>
                                </TooltipTrigger>
                                <TooltipContent>
                                  {isExpanded
                                    ? "Hide Parameters"
                                    : "Show Parameters"}
                                </TooltipContent>
                              </Tooltip>
                            </TooltipProvider>
                          </div>
                        </TableCell>
                      </TableRow>

                      {/* Expanded parameters section */}
                      {isExpanded && (
                        <TableRow key={`${workflowRun.workflow_run_id}-params`}>
                          <TableCell
                            colSpan={4}
                            className="bg-slate-50 dark:bg-slate-900/50"
                          >
                            <WorkflowRunParameters
                              workflowPermanentId={workflowPermanentId}
                              workflowRunId={workflowRun.workflow_run_id}
                              workflow={workflow}
                              searchQuery={debouncedSearch}
                              keywordMatchesParameter={matchesParameter}
                            />
                          </TableCell>
                        </TableRow>
                      )}
                    </React.Fragment>
                  );
                })
              )}
            </TableBody>
          </Table>
          <RunParametersDialog
            open={openRunParams !== null}
            onOpenChange={(open) => {
              if (!open) setOpenRunParams(null);
            }}
            workflowPermanentId={workflowPermanentId}
            workflowRunId={openRunParams}
          />
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
                  className={cn({
                    "cursor-not-allowed":
                      workflowRuns !== undefined &&
                      workflowRuns.length < PAGE_SIZE,
                  })}
                  onClick={() => {
                    if (workflowRuns && workflowRuns.length < PAGE_SIZE) {
                      return;
                    }
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
    </div>
  );
}

type WorkflowRunParametersProps = {
  workflowPermanentId: string;
  workflowRunId: string;
  workflow: Awaited<ReturnType<typeof useWorkflowQuery>>["data"];
  searchQuery: string;
  keywordMatchesParameter: (parameter: {
    key: string;
    value: unknown;
    description?: string | null;
  }) => boolean;
};

function WorkflowRunParameters({
  workflowPermanentId,
  workflowRunId,
  workflow,
  searchQuery,
  keywordMatchesParameter,
}: WorkflowRunParametersProps) {
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const credentialGetter = useCredentialGetter();

  const isGlobalWorkflow =
    globalWorkflows?.some(
      (wf) => wf.workflow_permanent_id === workflowPermanentId,
    ) ?? false;

  const { data: run, isLoading } = useQuery<WorkflowRunStatusApiResponse>({
    queryKey: [
      "workflowRun",
      workflowPermanentId,
      workflowRunId,
      "params",
      isGlobalWorkflow,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      if (isGlobalWorkflow) {
        params.set("template", "true");
      }
      return client
        .get(`/workflows/${workflowPermanentId}/runs/${workflowRunId}`, {
          params,
        })
        .then((r) => r.data);
    },
    enabled: Boolean(workflowPermanentId && workflowRunId),
  });

  if (isLoading) {
    return (
      <div className="ml-8 py-4">
        <Skeleton className="h-20 w-full" />
      </div>
    );
  }

  if (!run || !run.parameters || Object.keys(run.parameters).length === 0) {
    return (
      <div className="ml-8 py-4 text-sm text-slate-400">
        No parameters for this run
      </div>
    );
  }

  // Create a map of parameter definitions by key
  const defByKey = new Map(
    (workflow?.workflow_definition.parameters ?? []).map((p) => [p.key, p]),
  );

  const parameterItems = Object.entries(run.parameters).map(([key, value]) => {
    const def = defByKey.get(key);
    const description = def && "description" in def ? def.description : null;
    return {
      key,
      value,
      description,
    };
  });

  return (
    <ParameterDisplayInline
      parameters={parameterItems}
      searchQuery={searchQuery}
      keywordMatchesParameter={keywordMatchesParameter}
    />
  );
}

export { WorkflowPage };
