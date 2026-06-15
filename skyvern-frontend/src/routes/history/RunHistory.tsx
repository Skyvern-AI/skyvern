import {
  ExclamationTriangleIcon,
  LightningBoltIcon,
  MixerHorizontalIcon,
  RocketIcon,
} from "@radix-ui/react-icons";

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
  TriggerType,
  WorkflowRunStatusApiResponse,
} from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { StatusFilterDropdown } from "@/components/StatusFilterDropdown";
import { TriggerTypeBadge } from "@/components/TriggerTypeBadge";
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
  TableMessageRow,
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
import React, { useMemo, useState } from "react";
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
import { useOnboardingStateOptional } from "@/store/onboarding/useOnboardingState";
import { OnboardingEmptyState } from "@/components/onboarding/OnboardingEmptyState";
import { useFeatureFlagVariantKey } from "posthog-js/react";
import { EXPERIMENT, isABVariant } from "@/util/onboarding/experimentConfig";

const statusValues = new Set<string>(Object.values(Status));
function isKnownStatus(value: string): value is Status {
  return statusValues.has(value);
}

function parseStatusParam(raw: string | null): Array<Status> {
  if (!raw) {
    return [];
  }
  const seen = new Set<Status>();
  const out: Array<Status> = [];
  for (const token of raw.split(",")) {
    const trimmed = token.trim();
    if (trimmed === "" || !isKnownStatus(trimmed) || seen.has(trimmed)) {
      continue;
    }
    seen.add(trimmed);
    out.push(trimmed);
  }
  return out;
}

// Scheduled workflow runs carry a deterministic `wr_sched_<hash>` id prefix.
function inferTriggerType(run: TaskRunListItem): TriggerType | null {
  if (
    run.task_run_type === TaskRunType.WorkflowRun &&
    run.run_id.startsWith("wr_sched_")
  ) {
    return TriggerType.Scheduled;
  }
  return null;
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
  const onboarding = useOnboardingStateOptional();
  const isNewUser = onboarding?.isNewUser ?? false;
  const onboardingState = onboarding?.state ?? null;
  const onboardingFlag = useFeatureFlagVariantKey(EXPERIMENT.flagKey);
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const itemsPerPage = searchParams.get("page_size")
    ? Number(searchParams.get("page_size"))
    : 10;
  const workflowPermanentIdFilter = searchParams.get("workflow_permanent_id");
  const statusFilters = useMemo(
    () => parseStatusParam(searchParams.get("status")),
    [searchParams],
  );
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 500);

  const effectiveSearch = workflowPermanentIdFilter || debouncedSearch;

  const { data: rawRuns, isFetching } = useRunsQuery({
    page,
    pageSize: itemsPerPage,
    statusFilters,
    search: effectiveSearch,
  });
  const navigate = useNavigate();

  const { data: rawNextPageRuns } = useRunsQuery({
    page: page + 1,
    pageSize: itemsPerPage,
    statusFilters,
    search: effectiveSearch,
    enabled: rawRuns?.length === itemsPerPage,
  });

  // /runs treats `search` as a substring match across searchable_text,
  // run_id, and workflow_permanent_id. When the user is filtering by a
  // specific workflow_permanent_id we tighten the result client-side so
  // unrelated runs whose text shares a substring with this id don't bleed in.
  // Pagination becomes best-effort under this filter — pages may be shorter
  // than itemsPerPage when matches are sparse.
  const runs = useMemo(() => {
    if (!rawRuns || !workflowPermanentIdFilter) {
      return rawRuns;
    }
    return rawRuns.filter(
      (run) => run.workflow_permanent_id === workflowPermanentIdFilter,
    );
  }, [rawRuns, workflowPermanentIdFilter]);

  const nextPageRuns = useMemo(() => {
    if (!rawNextPageRuns || !workflowPermanentIdFilter) {
      return rawNextPageRuns;
    }
    return rawNextPageRuns.filter(
      (run) => run.workflow_permanent_id === workflowPermanentIdFilter,
    );
  }, [rawNextPageRuns, workflowPermanentIdFilter]);

  const isNextDisabled =
    isFetching || !nextPageRuns || nextPageRuns.length === 0;

  const { matchesParameter } = useKeywordSearch(debouncedSearch);
  const { expandedRows, toggleExpanded: toggleParametersExpanded } =
    useParameterExpansion();

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
      return <TableMessageRow colSpan={6}>No runs found</TableMessageRow>;
    }

    return runs?.map((run, index) => {
      const executionTime = formatExecutionTime(
        run.started_at ?? run.created_at,
        run.finished_at,
      );
      const isWorkflowRun = run.task_run_type === TaskRunType.WorkflowRun;
      const isExpanded = isWorkflowRun && expandedRows.has(run.run_id);
      const navPath = getRunNavigationPath(run);
      const triggerType = inferTriggerType(run);

      const titleContent =
        triggerType || run.script_run || run.workflow_deleted ? (
          <div className="flex items-center gap-2">
            {triggerType && <TriggerTypeBadge triggerType={triggerType} />}
            {run.script_run && (
              <Tip content="Ran with code">
                <LightningBoltIcon className="text-[gold]" />
              </Tip>
            )}
            {run.workflow_deleted && (
              <Tip content="Source agent deleted">
                <ExclamationTriangleIcon className="text-amber-400" />
              </Tip>
            )}
            <span
              className={cn(
                run.workflow_deleted && "text-neutral-600 dark:text-slate-400",
                "truncate",
              )}
            >
              {run.title ?? ""}
            </span>
          </div>
        ) : (
          (run.title ?? "")
        );

      return (
        <React.Fragment key={run.task_run_id}>
          <TableRow
            className="cursor-pointer"
            data-hint={index === 0 ? "run-recording" : undefined}
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
                <span className="text-sm text-neutral-600 dark:text-slate-400">
                  {run.status}
                </span>
              )}
            </TableCell>
            <TableCell
              className="max-w-0 truncate"
              title={basicTimeFormat(run.created_at)}
            >
              {basicLocalTimeFormat(run.created_at)}
            </TableCell>
            <TableCell className="text-neutral-600 dark:text-slate-400">
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
                          variant="ghost"
                          onClick={(event) => {
                            event.stopPropagation();
                            toggleParametersExpanded(run.run_id);
                          }}
                          className={cn(
                            "text-muted-foreground hover:text-foreground",
                            isExpanded && "text-blue-400",
                          )}
                        >
                          <MixerHorizontalIcon className="h-4 w-4" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>
                        {isExpanded ? "Hide Inputs" : "Show Inputs"}
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

  function clearWorkflowFilter() {
    const params = new URLSearchParams(searchParams);
    params.delete("workflow_permanent_id");
    params.set("page", "1");
    setSearchParams(params, { replace: true });
  }

  const hasActiveFilters =
    statusFilters.length > 0 ||
    !!debouncedSearch ||
    !!workflowPermanentIdFilter;
  const showOnboardingEmpty =
    !isFetching &&
    runs?.length === 0 &&
    !hasActiveFilters &&
    isNewUser &&
    onboardingState?.first_run_at === null &&
    isABVariant(onboardingFlag);

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl">Run History</h1>
      </header>
      {showOnboardingEmpty ? (
        <div className="rounded-lg border">
          <OnboardingEmptyState
            surface="runs"
            icon={<RocketIcon className="h-6 w-6" />}
            title="Your run history will appear here"
            description="Every time you run a workflow, the result shows up on this page. Create your first workflow to get started."
            primaryAction={{
              label: "Create your first workflow",
              onClick: () => navigate("/workflows"),
            }}
            secondaryAction={{
              label: "Browse templates",
              onClick: () => navigate("/workflows"),
            }}
          />
        </div>
      ) : (
        <>
          {workflowPermanentIdFilter ? (
            <div
              className="flex items-center justify-between gap-2 rounded-md border border-dashed bg-muted/30 px-3 py-2 text-xs"
              data-testid="workflow-filter-banner"
            >
              <span className="truncate">
                Filtering runs for workflow{" "}
                <span className="font-mono">{workflowPermanentIdFilter}</span>
              </span>
              <Button
                variant="ghost"
                size="sm"
                onClick={clearWorkflowFilter}
                className="h-auto py-1 text-xs"
              >
                Clear
              </Button>
            </div>
          ) : null}
          <div className="flex items-center justify-between gap-4">
            <TableSearchInput
              value={search}
              onChange={(value) => {
                setSearch(value);
                const params = new URLSearchParams(searchParams);
                params.set("page", "1");
                setSearchParams(params, { replace: true });
              }}
              placeholder={
                workflowPermanentIdFilter
                  ? "Clear the agent filter above to search"
                  : "Search by run ID or input..."
              }
              disabled={!!workflowPermanentIdFilter}
              className="w-48 lg:w-72"
            />
            <StatusFilterDropdown
              values={statusFilters}
              onChange={(filters) => {
                const params = new URLSearchParams(searchParams);
                if (filters.length === 0) {
                  params.delete("status");
                } else {
                  params.set("status", filters.join(","));
                }
                params.set("page", "1");
                setSearchParams(params, { replace: true });
              }}
            />
          </div>
          <div className="overflow-hidden rounded-lg border border-border">
            <Table className="sm:table-fixed">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[20%]">Run ID</TableHead>
                  <TableHead className="w-[20%]">Detail</TableHead>
                  <TableHead className="w-[16%]">Status</TableHead>
                  <TableHead className="w-[27%]">Created At</TableHead>
                  <TableHead className="w-[8%]">Duration</TableHead>
                  <TableHead className="w-[8%]"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>{displayTableBody()}</TableBody>
            </Table>
            <div className="relative px-3 py-3">
              <div className="absolute left-3 top-1/2 flex -translate-y-1/2 items-center gap-2 text-sm">
                <span className="text-neutral-600 dark:text-slate-400">
                  Items per page
                </span>
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
        </>
      )}
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
      <div className="ml-8 py-4 text-sm text-neutral-600 dark:text-slate-400">
        No inputs for this run
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
          title="Run Inputs"
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
