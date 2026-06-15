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
import React, { useContext, useEffect, useMemo, useState } from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useWorkflowQuery } from "./hooks/useWorkflowQuery";
import { useWorkflowRunsQuery } from "./hooks/useWorkflowRunsQuery";
import { useTagKeysQuery } from "./hooks/useTagKeysQuery";
import { useWorkflowTagsBatchQuery } from "./hooks/useWorkflowTagsBatchQuery";
import { TagChipList } from "./components/tagging/TagChipList";
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
import { useQuery } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useGlobalWorkflowsQuery } from "./hooks/useGlobalWorkflowsQuery";
import { TableSearchInput } from "@/components/TableSearchInput";
import { useKeywordSearch } from "./hooks/useKeywordSearch";
import { useParameterExpansion } from "./hooks/useParameterExpansion";
import { ParameterDisplayInline } from "./components/ParameterDisplayInline";
import { getOrderedRunParameters } from "./utils";
import { buildWorkflowAnalyticsPath } from "./workflowAnalyticsPath";
import {
  useFeatureFlagEnabled,
  useFeatureFlagVariantKey,
} from "posthog-js/react";
import { EXPERIMENT, isABVariant } from "@/util/onboarding/experimentConfig";
import { ANALYTICS_DASHBOARD_FLAG } from "@/util/featureFlags";
import { useOnboardingStateOptional } from "@/store/onboarding/useOnboardingState";
import { OnboardingEmptyState } from "@/components/onboarding/OnboardingEmptyState";

function WorkflowPage() {
  const { workflowPermanentId } = useParams();
  const isCloud = useContext(CloudContext);
  const onboarding = useOnboardingStateOptional();
  const isNewUser = onboarding?.isNewUser ?? false;
  const onboardingState = onboarding?.state ?? null;
  const onboardingFlag = useFeatureFlagVariantKey(EXPERIMENT.flagKey);
  const analyticsEnabled =
    useFeatureFlagEnabled(ANALYTICS_DASHBOARD_FLAG) === true;
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const [statusFilters, setStatusFilters] = useState<Array<Status>>([]);
  const navigate = useNavigate();

  const PAGE_SIZE_OPTIONS = ["10", "25", "50"];
  const pageSize = Number(searchParams.get("page_size") || "10");
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 500);
  const [openRunParams, setOpenRunParams] = useState<string | null>(null);
  const { matchesParameter } = useKeywordSearch(debouncedSearch);
  const { expandedRows, toggleExpanded: toggleParametersExpanded } =
    useParameterExpansion();

  const { data: workflowRuns, isLoading } = useWorkflowRunsQuery({
    workflowPermanentId,
    statusFilters,
    page,
    pageSize,
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

  const { data: workflowTagsMap = {} } = useWorkflowTagsBatchQuery(
    workflowPermanentId ? [workflowPermanentId] : [],
  );
  const workflowTags = workflowPermanentId
    ? workflowTagsMap[workflowPermanentId]
    : undefined;
  const { data: tagKeys = [] } = useTagKeysQuery();
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

  if (!workflowPermanentId) {
    return null; // this should never happen
  }

  return (
    <div className="space-y-8">
      <header className="flex justify-between">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
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
          {!workflowIsLoading && workflowTags && workflowTags.length > 0 ? (
            <TagChipList
              tags={workflowTags}
              descriptions={tagDescriptions}
              maxVisible={6}
            />
          ) : null}
        </div>
        <div className="flex gap-2">
          {workflow && (
            <WorkflowActions
              workflow={workflow}
              onSuccessfullyDeleted={() => navigate("/workflows")}
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
            <Link to={`/workflows/${workflowPermanentId}/scripts`}>
              <CodeIcon className="mr-2 size-4" />
              Scripts
            </Link>
          </Button>
          <Button asChild variant="secondary">
            <Link
              to={`/workflows/${workflowPermanentId}/build`}
              data-testid="workflow-open-editor-link"
            >
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
        {!isLoading &&
        workflowRuns?.length === 0 &&
        statusFilters.length === 0 &&
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
                onClick: () =>
                  navigate(`/workflows/${workflowPermanentId}/run`),
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
              <StatusFilterDropdown
                values={statusFilters}
                onChange={setStatusFilters}
              />
            </div>
            <div className="overflow-hidden rounded-lg border border-border">
              <Table>
                <TableHeader>
                  <TableRow>
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
                    <TableMessageRow colSpan={5}>Loading runs…</TableMessageRow>
                  ) : workflowRuns?.length === 0 ? (
                    <TableMessageRow colSpan={5}>
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
                            <TableCell className="font-mono text-xs text-muted-foreground">
                              {workflowRunId}
                            </TableCell>
                            <TableCell>
                              <StatusBadge status={workflowRun.status} />
                            </TableCell>
                            <TableCell
                              className="text-muted-foreground"
                              title={basicTimeFormat(workflowRun.created_at)}
                            >
                              {compactLocalDateTime(workflowRun.created_at)}
                            </TableCell>
                            <TableCell className="tabular-nums text-muted-foreground">
                              {formatExecutionTime(
                                workflowRun.started_at ??
                                  workflowRun.created_at,
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
                                            ? "text-blue-400"
                                            : "text-muted-foreground hover:text-foreground",
                                        )}
                                      >
                                        <MixerHorizontalIcon className="h-4 w-4" />
                                      </Button>
                                    </TooltipTrigger>
                                    <TooltipContent>
                                      {isExpanded
                                        ? "Hide Inputs"
                                        : "Show Inputs"}
                                    </TooltipContent>
                                  </Tooltip>
                                </TooltipProvider>
                              </div>
                            </TableCell>
                          </TableRow>

                          {/* Expanded parameters section */}
                          {isExpanded && (
                            <TableRow
                              key={`${workflowRun.workflow_run_id}-params`}
                            >
                              <TableCell
                                colSpan={5}
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
              <div className="relative px-3 py-3">
                <div className="absolute left-3 top-1/2 flex -translate-y-1/2 items-center gap-2 text-sm">
                  <span className="text-slate-400">Items per page</span>
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
      <div className="ml-8 py-4 text-sm text-slate-400">
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
