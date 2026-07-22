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
import { AgentFilterDropdown } from "@/components/AgentFilterDropdown";
import {
  RunTypeFilterDropdown,
  RunTypeGroup,
  runTypeGroupToRunTypes,
} from "@/components/RunTypeFilterDropdown";
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
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
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
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
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
import { useRunTagsBatchQuery } from "@/routes/tasks/hooks/useRunTagsBatchQuery";
import { useRunsHealSummaryBatchQuery } from "@/routes/workflows/hooks/useRunsHealSummaryBatchQuery";
import { RunOutcomeRiskMarker } from "@/routes/workflows/workflowRun/RunOutcomeRiskMarker";
import { useRunTagSuggestionsQuery } from "@/routes/tasks/hooks/useRunTagSuggestionsQuery";
import { useTagKeysQuery } from "@/routes/workflows/hooks/useTagKeysQuery";
import { useTagValuesQuery } from "@/routes/workflows/hooks/useTagValuesQuery";
import { TagChipList } from "@/routes/workflows/components/tagging/TagChipList";
import { TagFilterControl } from "@/routes/workflows/components/tagging/TagFilterControl";
import { useRunTagFilterParam } from "@/routes/workflows/hooks/useRunTagFilterParam";
import { WORKFLOW_TAGGING_FLAG } from "@/util/featureFlags";
import {
  SelectionCheckboxCell,
  SelectionHeaderCheckboxCell,
} from "@/components/SelectionCheckbox";
import { useRowSelection } from "@/hooks/useRowSelection";
import { RunBulkActionBar } from "@/routes/runs/RunBulkActionBar";
import { RunRowContextMenu } from "@/routes/runs/RunRowContextMenu";

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

const runTypeGroupValues = new Set<string>(Object.values(RunTypeGroup));

function parseRunTypeParam(raw: string | null): Array<RunTypeGroup> {
  if (!raw) {
    return [];
  }
  const seen = new Set<RunTypeGroup>();
  const out: Array<RunTypeGroup> = [];
  for (const token of raw.split(",")) {
    const trimmed = token.trim();
    if (!runTypeGroupValues.has(trimmed)) {
      continue;
    }
    const group = trimmed as RunTypeGroup;
    if (seen.has(group)) {
      continue;
    }
    seen.add(group);
    out.push(group);
  }
  return out;
}

function parseAgentParam(raw: string | null): Array<string> {
  if (!raw) {
    return [];
  }
  return [
    ...new Set(
      raw
        .split(",")
        .map((token) => token.trim())
        .filter(Boolean),
    ),
  ];
}

// Scheduled workflow runs carry a deterministic `wr_sched_<hash>` id prefix.
function inferTriggerType(run: TaskRunListItem): TriggerType | null {
  if (run.trigger_type) {
    return run.trigger_type;
  }
  if (
    run.task_run_type === TaskRunType.WorkflowRun &&
    run.run_id.startsWith("wr_sched_")
  ) {
    return TriggerType.Scheduled;
  }
  return null;
}

function getRunNavigationPath(
  run: TaskRunListItem,
  studioEnabled: boolean,
): string {
  switch (run.task_run_type) {
    case TaskRunType.WorkflowRun:
      // With the studio on, workflow runs open in its Run tab; otherwise they
      // use the standalone run page (also the fallback when there is no wpid).
      return studioEnabled && run.workflow_permanent_id
        ? `/agents/${run.workflow_permanent_id}/studio?wr=${run.run_id}`
        : `/runs/${run.run_id}`;
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
  const agentFilters = useMemo(() => {
    const filters = parseAgentParam(searchParams.get("agent"));
    const legacyFilter = searchParams.get("workflow_permanent_id")?.trim();
    if (legacyFilter && !filters.includes(legacyFilter)) {
      filters.push(legacyFilter);
    }
    return filters;
  }, [searchParams]);
  const workflowPermanentIds =
    agentFilters.length > 0 ? agentFilters : undefined;
  const statusFilters = useMemo(
    () => parseStatusParam(searchParams.get("status")),
    [searchParams],
  );
  const runTypeGroups = useMemo(
    () => parseRunTypeParam(searchParams.get("run_type")),
    [searchParams],
  );
  const runTypeFilters = useMemo(
    () => runTypeGroups.flatMap((group) => runTypeGroupToRunTypes[group]),
    [runTypeGroups],
  );
  const { tagTerms, tagsParam, writeTagsParam } = useRunTagFilterParam(
    searchParams,
    setSearchParams,
  );
  const taggingEnabled = useFeatureFlag(WORKFLOW_TAGGING_FLAG) !== false;
  // A stale ?tags= URL param would 403 the request when tagging is disabled.
  const effectiveTagsParam = taggingEnabled ? tagsParam : undefined;
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 500);

  // The /runs search_key requires min 3 chars (trigram index); shorter queries 422.
  const trimmedSearch = debouncedSearch.trim();
  const textSearch = trimmedSearch.length >= 3 ? trimmedSearch : "";

  const {
    data: runs,
    isFetching,
    isError,
    refetch,
  } = useRunsQuery({
    page,
    pageSize: itemsPerPage,
    statusFilters,
    runTypeFilters,
    search: textSearch,
    tags: effectiveTagsParam,
    workflowPermanentIds,
  });
  const navigate = useNavigate();
  const studioEnabled = useWorkflowStudioEnabled();

  const { data: nextPageRuns } = useRunsQuery({
    page: page + 1,
    pageSize: itemsPerPage,
    statusFilters,
    runTypeFilters,
    search: textSearch,
    tags: effectiveTagsParam,
    workflowPermanentIds,
    enabled: runs?.length === itemsPerPage,
  });

  const isNextDisabled =
    isFetching || !nextPageRuns || nextPageRuns.length === 0;

  const runIds = useMemo(
    () =>
      (runs ?? [])
        .filter((run) => run.task_run_type === TaskRunType.WorkflowRun)
        .map((run) => run.run_id),
    [runs],
  );
  const { data: runTagsMap = {} } = useRunTagsBatchQuery(runIds, {
    enabled: taggingEnabled,
  });
  const { data: runHealMap = {} } = useRunsHealSummaryBatchQuery(runIds);
  const { data: tagKeys = [] } = useTagKeysQuery({ enabled: taggingEnabled });
  const tagDescriptions = useMemo(
    () =>
      new Map(
        tagKeys.map((tagKey): [string, string | null] => [
          tagKey.key,
          tagKey.description,
        ]),
      ),
    [tagKeys],
  );
  const { data: tagColors } = useTagValuesQuery({ enabled: taggingEnabled });
  const { data: runTagSuggestions } = useRunTagSuggestionsQuery({
    enabled: taggingEnabled,
  });
  const tagFilterKeys = useMemo(
    () =>
      (runTagSuggestions?.keys ?? []).map((key) => ({
        key,
        description: null,
        workflow_count: 0,
      })),
    [runTagSuggestions?.keys],
  );
  const selectableRuns = useMemo(
    () =>
      taggingEnabled
        ? (runs ?? []).filter(
            (run) => run.task_run_type === TaskRunType.WorkflowRun,
          )
        : [],
    [runs, taggingEnabled],
  );
  const showCheckbox = selectableRuns.length > 0;
  const columnCount = showCheckbox ? 7 : 6;
  const {
    selectedItems: selectedRuns,
    isSelected,
    allSelected,
    someSelected,
    indexById: selectableIndexById,
    handleSelect,
    toggleSelectAll,
    clearSelection,
  } = useRowSelection({
    items: selectableRuns,
    getId: (run) => run.run_id,
    resetKey: JSON.stringify([
      page,
      statusFilters,
      runTypeGroups,
      tagsParam,
      agentFilters,
      textSearch,
      taggingEnabled,
    ]),
    anchorResetKey: itemsPerPage,
  });

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
          <TableCell colSpan={columnCount}>
            <Skeleton className="h-4 w-full" />
          </TableCell>
        </TableRow>
      ));
    }

    // Failed to load runs
    if (isError) {
      return (
        <TableMessageRow colSpan={columnCount}>
          <div className="flex items-center justify-center gap-3">
            <span>Failed to load runs.</span>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                refetch();
              }}
            >
              Retry
            </Button>
          </div>
        </TableMessageRow>
      );
    }

    // No runs found
    if (runs?.length === 0) {
      return (
        <TableMessageRow colSpan={columnCount}>No runs found</TableMessageRow>
      );
    }

    return runs?.map((run, index) => {
      const executionTime = formatExecutionTime(
        run.started_at ?? run.created_at,
        run.finished_at,
      );
      const isWorkflowRun = run.task_run_type === TaskRunType.WorkflowRun;
      const isExpanded = isWorkflowRun && expandedRows.has(run.run_id);
      const navPath = getRunNavigationPath(run, studioEnabled);
      const triggerType = inferTriggerType(run);
      const runTags = runTagsMap[run.run_id];
      const selectableIndex = selectableIndexById.get(run.run_id) ?? -1;
      const isRowSelected = isSelected(run.run_id);
      const taggable = taggingEnabled && isWorkflowRun;

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

      const mainRow = (
        <TableRow
          className="group/row cursor-pointer select-none"
          data-state={isRowSelected ? "selected" : undefined}
          data-hint={index === 0 ? "run-recording" : undefined}
          onClick={(event) => {
            handleNavigate(event, navPath);
          }}
        >
          {showCheckbox &&
            (taggable ? (
              <SelectionCheckboxCell
                index={selectableIndex}
                checked={isRowSelected}
                hasSelection={selectedRuns.length > 0}
                onSelect={handleSelect}
                ariaLabel={`Select ${run.title ?? run.run_id}`}
              />
            ) : (
              <TableCell />
            ))}
          <TableCell className="max-w-0 truncate" title={run.run_id}>
            <HighlightText text={run.run_id} query={textSearch} />
          </TableCell>
          <TableCell
            className="max-w-0 truncate"
            title={run.title ?? undefined}
          >
            <div className="flex min-w-0 items-center gap-2">
              <div className="min-w-0 truncate">{titleContent}</div>
              {taggingEnabled && runTags && runTags.length > 0 ? (
                <TagChipList
                  tags={runTags}
                  descriptions={tagDescriptions}
                  colors={tagColors}
                  maxVisible={2}
                  hideSystemTags
                  compact
                  className="shrink-0"
                />
              ) : null}
            </div>
          </TableCell>
          <TableCell>
            <div className="flex items-center gap-1.5">
              {isKnownStatus(run.status) ? (
                <StatusBadge status={run.status} />
              ) : (
                <span className="text-sm text-neutral-600 dark:text-slate-400">
                  {run.status}
                </span>
              )}
              <RunOutcomeRiskMarker
                outcomeRisk={
                  (runHealMap[run.run_id]?.blocks_outcome_risk?.length ?? 0) > 0
                }
              />
            </div>
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
      );

      return (
        <React.Fragment key={run.task_run_id}>
          {taggable ? (
            <RunRowContextMenu
              workflowRunId={run.run_id}
              runPath={navPath}
              currentTags={runTags ?? []}
              tagKeys={tagFilterKeys}
              labelSuggestions={runTagSuggestions?.labels ?? []}
              valueSuggestionsByKey={runTagSuggestions?.valuesByKey}
              selectedCount={selectedRuns.length}
              onNavigate={navigate}
            >
              {mainRow}
            </RunRowContextMenu>
          ) : (
            mainRow
          )}
          {isExpanded && run.workflow_permanent_id && (
            <TableRow key={`${run.run_id}-params`}>
              <TableCell
                colSpan={columnCount}
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

  const hasActiveFilters =
    statusFilters.length > 0 ||
    runTypeGroups.length > 0 ||
    tagTerms.length > 0 ||
    !!textSearch ||
    agentFilters.length > 0;
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
              onClick: () => navigate("/agents"),
            }}
            secondaryAction={{
              label: "Browse templates",
              onClick: () => navigate("/agents"),
            }}
          />
        </div>
      ) : (
        <>
          <div className="flex items-center justify-between gap-4">
            <div className="flex items-center gap-2">
              <TableSearchInput
                value={search}
                onChange={(value) => {
                  setSearch(value);
                  const params = new URLSearchParams(searchParams);
                  params.set("page", "1");
                  setSearchParams(params, { replace: true });
                }}
                placeholder="Search by run ID or input..."
                className="w-48 lg:w-72"
              />
              {taggingEnabled ? (
                <TagFilterControl
                  tagKeys={tagFilterKeys}
                  labelSuggestions={runTagSuggestions?.labels}
                  valueSuggestionsByKey={runTagSuggestions?.valuesByKey}
                  value={tagTerms}
                  onChange={writeTagsParam}
                  colors={tagColors}
                />
              ) : null}
            </div>
            <div className="flex items-center gap-2">
              <AgentFilterDropdown
                values={agentFilters}
                onChange={(filters) => {
                  const params = new URLSearchParams(searchParams);
                  if (filters.length === 0) {
                    params.delete("agent");
                  } else {
                    params.set("agent", filters.join(","));
                  }
                  params.delete("workflow_permanent_id");
                  params.set("page", "1");
                  setSearchParams(params, { replace: true });
                }}
              />
              <RunTypeFilterDropdown
                values={runTypeGroups}
                onChange={(groups) => {
                  const params = new URLSearchParams(searchParams);
                  if (groups.length === 0) {
                    params.delete("run_type");
                  } else {
                    params.set("run_type", groups.join(","));
                  }
                  params.set("page", "1");
                  setSearchParams(params, { replace: true });
                }}
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
          </div>
          <div className="overflow-hidden rounded-lg border border-border">
            <Table className="sm:table-fixed">
              <TableHeader>
                <TableRow className="group/header">
                  {showCheckbox && (
                    <SelectionHeaderCheckboxCell
                      allSelected={allSelected}
                      someSelected={someSelected}
                      hasSelection={selectedRuns.length > 0}
                      onToggleAll={toggleSelectAll}
                      ariaLabel="Select all workflow runs"
                      className="w-10"
                    />
                  )}
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
            {taggingEnabled && selectedRuns.length > 0 ? (
              <RunBulkActionBar
                selectedRunIds={selectedRuns.map((run) => run.run_id)}
                runTagsMap={runTagsMap}
                tagKeys={tagFilterKeys}
                labelSuggestions={runTagSuggestions?.labels ?? []}
                valueSuggestionsByKey={runTagSuggestions?.valuesByKey}
                onClearSelection={clearSelection}
              />
            ) : null}
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
    queryFn: async ({ signal }) => {
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
          signal,
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
