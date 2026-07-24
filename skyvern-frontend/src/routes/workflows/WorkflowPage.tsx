import {
  BarChartIcon,
  LightningBoltIcon,
  CodeIcon,
  MixerHorizontalIcon,
  Pencil2Icon,
  PlayIcon,
} from "@radix-ui/react-icons";

import { Tip } from "@/components/Tip";
import { Status, WorkflowRunStatusApiResponse } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { CredentialFallbackRetryBadge } from "@/components/CredentialFallbackRetryBadge";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import {
  basicTimeFormat,
  compactLocalDateTime,
  formatExecutionTime,
} from "@/util/timeFormat";
import { cn } from "@/util/utils";
import CloudContext from "@/store/CloudContext";
import React, { useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useWorkflowQuery } from "./hooks/useWorkflowQuery";
import { useWorkflowRunsQuery } from "./hooks/useWorkflowRunsQuery";
import { useTagKeysQuery } from "./hooks/useTagKeysQuery";
import { useTagValuesQuery } from "./hooks/useTagValuesQuery";
import { useWorkflowTagsBatchQuery } from "./hooks/useWorkflowTagsBatchQuery";
import { TagChipList } from "./components/tagging/TagChipList";
import { TagFilterControl } from "./components/tagging/TagFilterControl";
import { useRunTagFilterParam } from "./hooks/useRunTagFilterParam";
import { WorkflowActions } from "./WorkflowActions";
import { useDebounce } from "use-debounce";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { RunParametersDialog } from "./workflowRun/RunParametersDialog";
import { getClient } from "@/api/AxiosClient";
import { useQuery } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useGlobalWorkflowsQuery } from "./hooks/useGlobalWorkflowsQuery";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { legacyRunDetailPath, workflowEditorPath } from "./studioNavigation";
import { TableSearchInput } from "@/components/TableSearchInput";
import { useKeywordSearch } from "./hooks/useKeywordSearch";
import { useParameterExpansion } from "./hooks/useParameterExpansion";
import { ParameterDisplayInline } from "./components/ParameterDisplayInline";
import { getOrderedRunParameters } from "./utils";
import { buildWorkflowAnalyticsPath } from "./workflowAnalyticsPath";
import { useFeatureFlagVariantKey } from "posthog-js/react";
import { EXPERIMENT, isABVariant } from "@/util/onboarding/experimentConfig";
import { WORKFLOW_TAGGING_FLAG } from "@/util/featureFlags";
import { useAnalyticsDashboardFlag } from "@/hooks/useAnalyticsDashboardFlag";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useOnboardingStateOptional } from "@/store/onboarding/useOnboardingState";
import { OnboardingEmptyState } from "@/components/onboarding/OnboardingEmptyState";
import { usePageSlots } from "@/store/PageSlots";
import { resolveRunWindow } from "./resolveRunWindow";
import { useRunTagsBatchQuery } from "@/routes/tasks/hooks/useRunTagsBatchQuery";
import { useRunTagSuggestionsQuery } from "@/routes/tasks/hooks/useRunTagSuggestionsQuery";
import {
  SelectionCheckboxCell,
  SelectionHeaderCheckboxCell,
} from "@/components/SelectionCheckbox";
import { useRowSelection } from "@/hooks/useRowSelection";
import { RunBulkActionBar } from "@/routes/runs/RunBulkActionBar";
import { RunRowContextMenu } from "@/routes/runs/RunRowContextMenu";
import { WorkflowReliabilityPanel } from "./workflowRun/WorkflowReliabilityPanel";
import { RunOutcomeRiskMarker } from "./workflowRun/RunOutcomeRiskMarker";
import { useRunsHealSummaryBatchQuery } from "./hooks/useRunsHealSummaryBatchQuery";

function WorkflowPage() {
  const { workflowPermanentId } = useParams();
  const isCloud = useContext(CloudContext);
  const onboarding = useOnboardingStateOptional();
  const isNewUser = onboarding?.isNewUser ?? false;
  const onboardingState = onboarding?.state ?? null;
  const onboardingFlag = useFeatureFlagVariantKey(EXPERIMENT.flagKey);
  const analyticsEnabled = useAnalyticsDashboardFlag() === true;
  const [searchParams, setSearchParams] = useSearchParams();
  // Snapped once on mount so the window stays stable across unrelated
  // searchParams changes (page flips, search) instead of re-sampling `now`.
  const runWindowNow = useRef(new Date());
  const runWindow = useMemo(
    () => resolveRunWindow(searchParams, runWindowNow.current),
    [searchParams],
  );
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const [statusFilters, setStatusFilters] = useState<Array<Status>>([]);
  const navigate = useNavigate();
  const studioEnabled = useWorkflowStudioEnabled();

  const PAGE_SIZE_OPTIONS = ["10", "25", "50"];
  const pageSize = Number(searchParams.get("page_size") || "10");
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 500);
  const [openRunParams, setOpenRunParams] = useState<string | null>(null);
  const { matchesParameter } = useKeywordSearch(debouncedSearch);
  const { expandedRows, toggleExpanded: toggleParametersExpanded } =
    useParameterExpansion();

  const { tagTerms, tagsParam, writeTagsParam } = useRunTagFilterParam(
    searchParams,
    setSearchParams,
  );

  // undefined (OSS / pre-load) shows tagging; only an explicit cloud `false` hides it.
  const taggingEnabled = useFeatureFlag(WORKFLOW_TAGGING_FLAG) !== false;

  const { data: workflowRuns, isLoading } = useWorkflowRunsQuery({
    workflowPermanentId,
    statusFilters,
    page,
    pageSize,
    search: debouncedSearch,
    createdAtStart: runWindow.createdAtStart,
    createdAtEnd: runWindow.createdAtEnd,
    // A stale ?tags= URL param would 403 the request when tagging is disabled.
    tags: taggingEnabled ? tagsParam : undefined,
    refetchOnMount: "always",
  });

  useEffect(() => {
    if (!isLoading && workflowRuns && workflowRuns.length === 0 && page > 1) {
      const params = new URLSearchParams(searchParams);
      params.set("page", String(page - 1));
      setSearchParams(params, { replace: true });
    }
  }, [workflowRuns, isLoading, page, searchParams, setSearchParams]);

  const { data: workflow, isLoading: workflowIsLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  const { data: workflowTagsMap = {} } = useWorkflowTagsBatchQuery(
    workflowPermanentId ? [workflowPermanentId] : [],
    { enabled: taggingEnabled },
  );
  const workflowTags = workflowPermanentId
    ? workflowTagsMap[workflowPermanentId]
    : undefined;
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
  const runIds = useMemo(
    () => (workflowRuns ?? []).map((r) => r.workflow_run_id),
    [workflowRuns],
  );
  const { data: runTagsMap = {} } = useRunTagsBatchQuery(runIds, {
    enabled: taggingEnabled,
  });
  const { data: runHealMap = {} } = useRunsHealSummaryBatchQuery(runIds);
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
  const selectableRuns = taggingEnabled ? (workflowRuns ?? []) : [];
  const showCheckbox = selectableRuns.length > 0;
  const columnCount = showCheckbox ? 6 : 5;
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
    getId: (run) => run.workflow_run_id,
    resetKey: JSON.stringify([
      page,
      statusFilters,
      debouncedSearch,
      tagsParam,
      runWindow.createdAtStart,
      runWindow.createdAtEnd,
      workflowPermanentId,
      taggingEnabled,
    ]),
    anchorResetKey: pageSize,
  });

  const {
    workflowAnalyticsPanel: WorkflowAnalyticsPanel,
    workflowRunsFilterControls: WorkflowRunsFilterControls,
  } = usePageSlots();

  if (!workflowPermanentId) {
    return null; // this should never happen
  }

  return (
    <div className="space-y-8">
      <header className="flex flex-col gap-4">
        <div className="flex justify-between">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
            <div className="flex flex-col gap-0.5">
              {workflowIsLoading ? (
                <>
                  <Skeleton className="h-7 w-56" />
                  <Skeleton className="h-7 w-56" />
                </>
              ) : (
                <>
                  <h1 className="text-lg font-semibold">{workflow?.title}</h1>
                  <h2 className="text-sm text-muted-foreground">
                    {workflowPermanentId}
                  </h2>
                </>
              )}
            </div>
            {taggingEnabled &&
            !workflowIsLoading &&
            workflowTags &&
            workflowTags.length > 0 ? (
              <TagChipList
                tags={workflowTags}
                descriptions={tagDescriptions}
                colors={tagColors}
                maxVisible={6}
              />
            ) : null}
          </div>
          <div className="flex gap-2">
            {workflow && (
              <WorkflowActions
                workflow={workflow}
                onSuccessfullyDeleted={() => navigate("/agents")}
              />
            )}
            {isCloud && analyticsEnabled ? (
              <Button asChild variant="secondary">
                <Link to={buildWorkflowAnalyticsPath(workflowPermanentId)}>
                  <BarChartIcon className="mr-2 size-4" />
                  Analytics
                </Link>
              </Button>
            ) : null}
            <Button asChild variant="secondary">
              <Link to={`/agents/${workflowPermanentId}/scripts`}>
                <CodeIcon className="mr-2 size-4" />
                Scripts
              </Link>
            </Button>
            <Button asChild variant="secondary">
              <Link
                to={workflowEditorPath(workflowPermanentId, studioEnabled)}
                data-testid="workflow-open-editor-link"
              >
                <Pencil2Icon className="mr-2 size-4" />
                Edit
              </Link>
            </Button>
            <Button asChild>
              <Link to={`/agents/${workflowPermanentId}/run`}>
                <PlayIcon className="mr-2 size-4" />
                Run
              </Link>
            </Button>
          </div>
        </div>
        <WorkflowReliabilityPanel workflowPermanentId={workflowPermanentId} />
        {WorkflowAnalyticsPanel ? (
          <WorkflowAnalyticsPanel workflowPermanentId={workflowPermanentId} />
        ) : null}
      </header>
      <div className="space-y-4">
        <header>
          <h1 className="text-2xl">Past Runs</h1>
        </header>
        {!isLoading &&
        workflowRuns?.length === 0 &&
        statusFilters.length === 0 &&
        tagTerms.length === 0 &&
        !debouncedSearch &&
        isNewUser &&
        onboardingState?.first_run_at === null &&
        isABVariant(onboardingFlag) ? (
          <div className="rounded-md border">
            <OnboardingEmptyState
              surface="runs"
              icon={<PlayIcon className="h-6 w-6" />}
              title="No runs yet for this workflow"
              description="Run this workflow to see results here. Each run tracks status, parameters, and duration."
              primaryAction={{
                label: "Run this workflow",
                onClick: () => navigate(`/agents/${workflowPermanentId}/run`),
              }}
              secondaryAction={{
                label: "View documentation",
                onClick: () =>
                  window.open(
                    "https://docs.skyvern.com",
                    "_blank",
                    "noopener,noreferrer",
                  ),
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
                  placeholder="Search runs by input..."
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
                {WorkflowRunsFilterControls ? (
                  <WorkflowRunsFilterControls />
                ) : null}
                <StatusFilterDropdown
                  values={statusFilters}
                  onChange={setStatusFilters}
                />
              </div>
            </div>
            <div className="overflow-hidden rounded-lg border border-border">
              <Table>
                <TableHeader>
                  <TableRow className="group/header">
                    {showCheckbox && (
                      <SelectionHeaderCheckboxCell
                        allSelected={allSelected}
                        someSelected={someSelected}
                        hasSelection={selectedRuns.length > 0}
                        onToggleAll={toggleSelectAll}
                        ariaLabel="Select all runs"
                        className="w-10"
                      />
                    )}
                    <TableHead className="w-[20%]">ID</TableHead>
                    <TableHead className="w-[20%]">Status</TableHead>
                    <TableHead className="w-[20%]">Created At</TableHead>
                    <TableHead className="w-[20%]">Duration</TableHead>
                    <TableHead className="w-[20%] text-right">
                      Actions
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {isLoading ? (
                    <TableMessageRow colSpan={columnCount}>
                      Loading runs…
                    </TableMessageRow>
                  ) : workflowRuns?.length === 0 ? (
                    <TableMessageRow colSpan={columnCount}>
                      No agent runs found
                    </TableMessageRow>
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
                          (workflowRun.workflow_run_id ?? "")
                        );

                      const isExpanded = expandedRows.has(
                        workflowRun.workflow_run_id,
                      );

                      const runTags = runTagsMap[workflowRun.workflow_run_id];
                      const selectableIndex =
                        selectableIndexById.get(workflowRun.workflow_run_id) ??
                        -1;
                      const isRowSelected = isSelected(
                        workflowRun.workflow_run_id,
                      );
                      const runPath = studioEnabled
                        ? `/runs/${workflowRun.workflow_run_id}`
                        : legacyRunDetailPath(
                            workflowPermanentId,
                            workflowRun.workflow_run_id,
                          );

                      const mainRow = (
                        <TableRow
                          onClick={(event) => {
                            if (event.ctrlKey || event.metaKey) {
                              window.open(
                                window.location.origin + runPath,
                                "_blank",
                                "noopener,noreferrer",
                              );
                              return;
                            }
                            navigate(runPath);
                          }}
                          className="group/row cursor-pointer select-none"
                          data-state={isRowSelected ? "selected" : undefined}
                        >
                          {showCheckbox && (
                            <SelectionCheckboxCell
                              index={selectableIndex}
                              checked={isRowSelected}
                              hasSelection={selectedRuns.length > 0}
                              onSelect={handleSelect}
                              ariaLabel={`Select ${workflowRun.workflow_run_id}`}
                            />
                          )}
                          <TableCell className="font-mono text-xs text-muted-foreground">
                            <div className="flex flex-col gap-1">
                              <span className="min-w-0 truncate">
                                {workflowRunId}
                              </span>
                              {taggingEnabled && runTags?.length ? (
                                <TagChipList
                                  tags={runTags}
                                  descriptions={tagDescriptions}
                                  colors={tagColors}
                                  maxVisible={2}
                                  hideSystemTags
                                  compact
                                  className="shrink-0 font-sans"
                                />
                              ) : null}
                            </div>
                          </TableCell>
                          <TableCell>
                            <div className="flex items-center gap-2">
                              <StatusBadge status={workflowRun.status} />
                              <CredentialFallbackRetryBadge
                                retriedFromWorkflowRunId={
                                  workflowRun.retried_from_workflow_run_id
                                }
                              />
                              <RunOutcomeRiskMarker
                                outcomeRisk={
                                  (runHealMap[workflowRun.workflow_run_id]
                                    ?.blocks_outcome_risk?.length ?? 0) > 0
                                }
                              />
                            </div>
                          </TableCell>
                          <TableCell
                            className="text-muted-foreground"
                            title={basicTimeFormat(workflowRun.created_at)}
                          >
                            {compactLocalDateTime(workflowRun.created_at)}
                          </TableCell>
                          <TableCell className="tabular-nums text-muted-foreground">
                            {formatExecutionTime(
                              workflowRun.started_at ?? workflowRun.created_at,
                              workflowRun.finished_at,
                            ) ?? "-"}
                          </TableCell>
                          <TableCell>
                            <div className="flex justify-end gap-2">
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      size="icon"
                                      variant="ghost"
                                      onClick={(event) => {
                                        event.stopPropagation();
                                        toggleParametersExpanded(
                                          workflowRun.workflow_run_id,
                                        );
                                      }}
                                      className={cn(
                                        isExpanded
                                          ? "text-blue-700 dark:text-blue-400"
                                          : "text-muted-foreground hover:text-foreground",
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
                          </TableCell>
                        </TableRow>
                      );

                      return (
                        <React.Fragment key={workflowRun.workflow_run_id}>
                          {taggingEnabled ? (
                            <RunRowContextMenu
                              workflowRunId={workflowRun.workflow_run_id}
                              runPath={runPath}
                              currentTags={runTags ?? []}
                              tagKeys={tagFilterKeys}
                              labelSuggestions={runTagSuggestions?.labels ?? []}
                              valueSuggestionsByKey={
                                runTagSuggestions?.valuesByKey
                              }
                              selectedCount={selectedRuns.length}
                              onNavigate={navigate}
                            >
                              {mainRow}
                            </RunRowContextMenu>
                          ) : (
                            mainRow
                          )}
                          {/* Expanded parameters section */}
                          {isExpanded && (
                            <TableRow
                              key={`${workflowRun.workflow_run_id}-params`}
                            >
                              <TableCell
                                colSpan={columnCount}
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
              {taggingEnabled && selectedRuns.length > 0 ? (
                <RunBulkActionBar
                  selectedRunIds={selectedRuns.map(
                    (run) => run.workflow_run_id,
                  )}
                  runTagsMap={runTagsMap}
                  tagKeys={tagFilterKeys}
                  labelSuggestions={runTagSuggestions?.labels ?? []}
                  valueSuggestionsByKey={runTagSuggestions?.valuesByKey}
                  onClearSelection={clearSelection}
                />
              ) : null}
              <RunParametersDialog
                open={openRunParams !== null}
                onOpenChange={(open) => {
                  if (!open) setOpenRunParams(null);
                }}
                workflowPermanentId={workflowPermanentId}
                workflowRunId={openRunParams}
              />
              <div className="relative px-3 py-3">
                <div className="absolute left-3 top-1/2 flex -translate-y-1/2 items-center gap-2 text-sm">
                  <span className="text-muted-foreground">Items per page</span>
                  <Select
                    value={String(pageSize)}
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
                      {PAGE_SIZE_OPTIONS.map((size) => (
                        <SelectItem key={size} value={size}>
                          {size}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <Pagination className="pt-0">
                  <PaginationContent>
                    <PaginationItem>
                      <PaginationPrevious
                        className={cn({ "cursor-not-allowed": page === 1 })}
                        onClick={() => {
                          if (page === 1) {
                            return;
                          }
                          const params = new URLSearchParams(searchParams);
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
                            workflowRuns.length < pageSize,
                        })}
                        onClick={() => {
                          if (workflowRuns && workflowRuns.length < pageSize) {
                            return;
                          }
                          const params = new URLSearchParams(searchParams);
                          params.set("page", String(page + 1));
                          setSearchParams(params, { replace: true });
                        }}
                      />
                    </PaginationItem>
                  </PaginationContent>
                </Pagination>
              </div>
            </div>
          </>
        )}
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
      <div className="ml-8 py-4 text-sm text-muted-foreground">
        No inputs for this run
      </div>
    );
  }

  // Create a map of parameter definitions by key
  const defByKey = new Map(
    (workflow?.workflow_definition.parameters ?? []).map((p) => [p.key, p]),
  );

  const parameterItems = getOrderedRunParameters(
    workflow?.workflow_definition.parameters,
    run.parameters,
  ).map(([key, value]) => {
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
