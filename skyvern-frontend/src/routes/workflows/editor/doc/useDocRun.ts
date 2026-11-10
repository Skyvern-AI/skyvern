import { useEffect, useMemo, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { AxiosError } from "axios";
import { getClient } from "@/api/AxiosClient";
import type { RunEngine, WorkflowRunApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useCopilotProposalStore } from "@/store/CopilotProposalStore";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";
import { useWorkflowPermanentId } from "../../WorkflowPermanentIdContext";
import { buildBlockStatusMap } from "../../studio/runProjections";

import { useWorkflowRunTimelineQuery } from "../../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../../hooks/useWorkflowRunWithWorkflowQuery";
import { useStudioRunId } from "../../studio/useStudioRunId";
import {
  getRunAttempt,
  runIsLogicallyActive,
} from "../../workflowRun/runRetryState";
import { filterTimelineToAttempt } from "../../workflowRun/workflowTimelineUtils";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../../types/workflowRunTypes";
import {
  collectBlockRuns,
  collectLoopProgress,
  type DocBlockRuns,
  docRunRowsSettled,
  ranAnotherVersion,
  type SavedVersion,
  settleRowsOfEndedRun,
  stepLabelsOf,
  stepsUnknownToRun,
  type DocLoopProgress,
} from "./docRunState";
import { useWorkflowQuery } from "../../hooks/useWorkflowQuery";
import type { WorkflowApiResponse } from "../../types/workflowTypes";
import { apiWorkflowToSettings } from "../apiWorkflowToSettings";
import {
  getElements,
  getWorkflowBlocks,
  workflowEffectiveDefaultEngine,
} from "../workflowEditorUtils";
import { docProduced } from "./docStepProduced";
import {
  useDismissedDocRunId,
  isStepRun,
  markStepRun,
  useDocRunStore,
  useStoredDocRunId,
} from "./docRunStore";

export function collectBlocks(
  items: WorkflowRunTimelineItem[],
  into: Map<string, WorkflowRunBlock>,
  workflowRunId: string,
) {
  for (const [label, state] of Object.entries(
    buildBlockStatusMap(items, workflowRunId),
  ))
    if (!into.has(label)) into.set(label, state.block);
}

const EMPTY = new Map<string, WorkflowRunBlock>();
const NO_LOOPS = new Map<string, DocLoopProgress>();
const NO_RUNS = new Map<string, DocBlockRuns>();
const NO_LABELS = new Set<string>();
type Projection = {
  // Whether the backend has moved every row off Running, before the Doc settles rows of an ended run.
  rowsSettled: boolean;
  blocks: Map<string, WorkflowRunBlock>;
  loops: Map<string, DocLoopProgress>;
  runs: Map<string, DocBlockRuns>;
};
// Every step calls useDocRun; walk the timeline once per poll, not once per step.
const walked = new WeakMap<object, WeakMap<object, Projection>>();
const NO_RUN = {};
type DocRunData = ReturnType<typeof useWorkflowRunWithWorkflowQuery>["data"];

function blocksFor(
  timeline: WorkflowRunTimelineItem[],
  run: DocRunData,
  workflowRunId: string,
) {
  const runKey = run ?? NO_RUN;
  let byRun = walked.get(timeline);
  if (!byRun) walked.set(timeline, (byRun = new WeakMap()));
  let projection = byRun.get(runKey);
  if (!projection) {
    const rows = filterTimelineToAttempt(
      timeline,
      run?.attempts ?? [],
      getRunAttempt(run ?? {}),
    );
    const attempt =
      run && !runIsLogicallyActive(run)
        ? settleRowsOfEndedRun(rows, run)
        : rows;
    const raw = new Map<string, WorkflowRunBlock>();
    collectBlocks(rows, raw, workflowRunId);
    projection = {
      rowsSettled: docRunRowsSettled(raw),
      blocks: new Map(),
      loops: new Map(),
      runs: new Map(),
    };
    collectBlocks(attempt, projection.blocks, workflowRunId);
    collectLoopProgress(attempt, workflowRunId, projection.loops);
    collectBlockRuns(
      attempt,
      workflowRunId,
      projection.runs,
      (block) => docProduced(block) !== null,
    );
    byRun.set(runKey, projection);
  }
  return projection;
}

// An editor save rewrites fields of steps it did not change (the title, an empty URL, the schema's form, a loop's
// source), so both versions are compared as the editor would save them: only a real edit hides a step's result.
type DefaultEngine = RunEngine | null | undefined;
const savedForm = new WeakMap<
  WorkflowApiResponse,
  Map<DefaultEngine, SavedVersion>
>();
function asEditorSaves(
  workflow: WorkflowApiResponse,
  effectiveDefaultEngine: DefaultEngine,
): SavedVersion {
  let byEngine = savedForm.get(workflow);
  if (!byEngine) savedForm.set(workflow, (byEngine = new Map()));
  let saved = byEngine.get(effectiveDefaultEngine);
  if (saved) return saved;
  saved = workflow;
  try {
    const { nodes, edges } = getElements(
      JSON.parse(JSON.stringify(workflow.workflow_definition.blocks)),
      apiWorkflowToSettings(workflow),
      false,
      effectiveDefaultEngine,
    );
    saved = {
      ...workflow,
      workflow_definition: {
        ...workflow.workflow_definition,
        blocks: getWorkflowBlocks(nodes, edges),
      },
    };
  } catch (error) {
    // A definition the editor cannot load is compared as stored.
    console.warn("Doc: comparing a version as stored", error);
  }
  byEngine.set(effectiveDefaultEngine, saved);
  return saved;
}

type RunWithWorkflow = NonNullable<DocRunData>;

/** The steps a run cannot speak for in the current version: added or edited since. */
function unknownToRun(
  run: RunWithWorkflow | undefined,
  current: WorkflowApiResponse | undefined,
  proposalPending: boolean,
  effectiveDefaultEngine: DefaultEngine,
): { earlierVersion: boolean; unknownLabels: Set<string> } {
  if (!run || !current)
    return { earlierVersion: false, unknownLabels: NO_LABELS };
  const ran = asEditorSaves(run.workflow, effectiveDefaultEngine);
  const now = asEditorSaves(current, effectiveDefaultEngine);
  const earlierVersion = ranAnotherVersion(ran, now, {
    copilotTest: Boolean(run.copilot_session_id),
    proposalPending,
  });
  return {
    earlierVersion,
    unknownLabels: earlierVersion
      ? stepsUnknownToRun(ran.workflow_definition, now.workflow_definition)
      : NO_LABELS,
  };
}

export const docLatestRunKey = (
  orgId: string | undefined,
  workflowPermanentId: string | undefined,
) =>
  getOrgScopedQueryKey(
    ["workflowRuns", "docLatest", workflowPermanentId],
    getActiveOrgQueryKeyScope(orgId),
  );

// Polls only while the tab is visible (react-query pauses interval refetches in a hidden tab).
const LATEST_RUN_POLL_MS = 15_000;
const LATEST_RUN_PAGE_SIZE = 20;
const MAX_LATEST_RUN_PAGES = 5;

// The agent's newest run from any browser or origin; every browser and tab reads the same one.
function useLatestRun(workflowPermanentId: string | undefined) {
  const credentialGetter = useCredentialGetter();
  const orgId = useActiveOrgId();
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: docLatestRunKey(orgId, workflowPermanentId),
    refetchInterval: LATEST_RUN_POLL_MS,
    refetchOnWindowFocus: "always",
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const fetchPage = async (page: number) =>
        (
          await client.get<
            Array<WorkflowRunApiResponse & { debug_session_id?: string | null }>
          >(`/workflows/${workflowPermanentId}/runs`, {
            params: { page, page_size: LATEST_RUN_PAGE_SIZE },
          })
        ).data;
      // A one-step (Play) run carries a debug session; it says nothing about the steps it did not run.
      const fullRuns = (runs: Awaited<ReturnType<typeof fetchPage>>) =>
        runs
          .filter((candidate) => !candidate.debug_session_id)
          .map((candidate) => candidate.workflow_run_id);
      let page = 1;
      let runs = await fetchPage(page);
      let ids = fullRuns(runs);
      // A full run on the first page is the answer. Without one, page back only while the query has no answer yet;
      // later polls keep that answer until the first page shows a newer full run.
      const earlier = queryClient.getQueryData<string[]>(
        docLatestRunKey(orgId, workflowPermanentId),
      );
      if (ids.length === 0 && earlier) {
        ids = earlier;
      } else if (ids.length === 0) {
        while (
          ids.length === 0 &&
          runs.length === LATEST_RUN_PAGE_SIZE &&
          page < MAX_LATEST_RUN_PAGES
        ) {
          runs = await fetchPage(++page);
          ids = fullRuns(runs);
        }
      }
      return ids;
    },
    enabled: Boolean(workflowPermanentId),
  });
}

type DocRunLayer = {
  runId: string | undefined;
  run: DocRunData;
  blocksByLabel: Map<string, WorkflowRunBlock>;
  loopsByLabel: Map<string, DocLoopProgress>;
  runsByLabel: Map<string, DocBlockRuns>;
  unknownLabels: Set<string>;
  ranLabels: Set<string> | null;
  earlierVersion: boolean;
  live: boolean;
  rowsLoaded: boolean;
  rowsSettled: boolean;
  owned: boolean;
  blockRun: boolean;
};

/**
 * The studio's inspected run, or else the agent's latest run, projected onto
 * the Doc: the latest block row per step label (a looped step keeps its last
 * iteration), each loop's per-item progress, and whether the run is live.
 */
export function useDocRun(): DocRunLayer & {
  sharedRunId: string | null;
  blockRunTarget: string | null;
  currentDefinition: unknown;
  currentVersion: number | undefined;
  proposalPending: boolean;
  previous: DocRunLayer | null;
} {
  const urlRunId = useStudioRunId();
  const orgId = useActiveOrgId();
  const workflowPermanentId = useWorkflowPermanentId();
  const storedRunId = useStoredDocRunId(orgId, workflowPermanentId);
  const dismissedRunId = useDismissedDocRunId(orgId, workflowPermanentId);
  const latest = useLatestRun(workflowPermanentId);
  // A run started here is written into the query, so the newest run always wins, wherever it was started.
  // Step (Play) runs started in this browser are skipped; the runs list marks no others (SKY-19814).
  const listed =
    latest.data === undefined
      ? storedRunId
      : (latest.data.find((id) => !isStepRun(id)) ?? null);
  const sharedRunId = listed === dismissedRunId ? null : listed;
  // This browser's own run stays on screen until it ends, even if a newer run starts elsewhere.
  const { data: storedRun } = useWorkflowRunWithWorkflowQuery({
    workflowRunId: storedRunId ?? undefined,
  });
  const pinnedRunId =
    storedRunId && storedRun && runIsLogicallyActive(storedRun)
      ? storedRunId
      : null;
  const runId = urlRunId ?? pinnedRunId ?? sharedRunId ?? undefined;
  // A one-step run (?bl=) says nothing about the steps it was never meant to run.
  const [searchParams] = useSearchParams();
  const blockRun = Boolean(urlRunId && searchParams.has("bl"));
  const blockRunTarget = blockRun ? searchParams.get("bl") : null;
  useEffect(() => {
    if (urlRunId && blockRun) markStepRun(urlRunId);
  }, [urlRunId, blockRun]);
  const { data: retained, isPlaceholderData } = useWorkflowRunTimelineQuery(
    runId ? { workflowRunId: runId } : undefined,
  );
  // A status change swaps the timeline query key; keep this run's last rows meanwhile
  // instead of blanking every result, but never show another run's or organization's.
  const lastTimeline = useRef<{
    runId: string | null | undefined;
    orgId: string | undefined;
    data: typeof retained;
  } | null>(null);
  if (!isPlaceholderData && retained)
    lastTimeline.current = { runId, orgId, data: retained };
  const timeline = isPlaceholderData
    ? lastTimeline.current &&
      lastTimeline.current.runId === runId &&
      lastTimeline.current.orgId === orgId
      ? lastTimeline.current.data
      : undefined
    : retained;
  const { data: run, error } = useWorkflowRunWithWorkflowQuery({
    workflowRunId: runId ?? undefined,
  });
  // A remembered run that is gone (deleted, another org's) is forgotten, not retried.
  const storedGone =
    !urlRunId &&
    Boolean(sharedRunId) &&
    error instanceof AxiosError &&
    error.response?.status === 404;
  useEffect(() => {
    if (storedGone && workflowPermanentId)
      useDocRunStore.getState().setDocRunId(orgId, workflowPermanentId, null);
  }, [storedGone, orgId, workflowPermanentId]);
  const projection = useMemo(
    () => (timeline && runId ? blocksFor(timeline, run, runId) : null),
    [timeline, run, runId],
  );
  const { data: current } = useWorkflowQuery({ workflowPermanentId });
  const queryClient = useQueryClient();
  // A property of the workflow, not the version: the run's version is read with the current one's.
  const effectiveDefaultEngine = current
    ? workflowEffectiveDefaultEngine(current, queryClient)
    : undefined;
  const proposal = useCopilotProposalStore((s) => s.proposal);
  const proposalPending = Boolean(
    proposal &&
    (!workflowPermanentId ||
      proposal.proposed.workflow_permanent_id === workflowPermanentId),
  );
  const { earlierVersion, unknownLabels } = useMemo(
    () => unknownToRun(run, current, proposalPending, effectiveDefaultEngine),
    [run, current, proposalPending, effectiveDefaultEngine],
  );
  // After a one-step run, the steps it did not run keep their result from the agent's latest full run.
  const previousRunId =
    blockRun && sharedRunId && sharedRunId !== runId ? sharedRunId : undefined;
  const { data: previousTimeline } = useWorkflowRunTimelineQuery({
    workflowRunId: previousRunId,
  });
  const { data: previousRun } = useWorkflowRunWithWorkflowQuery({
    workflowRunId: previousRunId,
  });
  const previous = useMemo((): DocRunLayer | null => {
    if (!previousRunId || !previousTimeline || !previousRun) return null;
    const projection = blocksFor(previousTimeline, previousRun, previousRunId);
    return {
      runId: previousRunId,
      run: previousRun,
      blocksByLabel: projection.blocks,
      loopsByLabel: projection.loops,
      runsByLabel: projection.runs,
      ...unknownToRun(
        previousRun,
        current,
        proposalPending,
        effectiveDefaultEngine,
      ),
      ranLabels: stepLabelsOf(previousRun.workflow.workflow_definition),
      live: runIsLogicallyActive(previousRun),
      rowsLoaded: true,
      rowsSettled: projection.rowsSettled,
      owned: previousRunId === storedRunId,
      blockRun: false,
    };
  }, [
    previousRunId,
    previousTimeline,
    previousRun,
    current,
    proposalPending,
    effectiveDefaultEngine,
    storedRunId,
  ]);
  // A step added since the run (even unsaved) was not in it, so it cannot be "not reached".
  const ranLabels = useMemo(
    () => (run ? stepLabelsOf(run.workflow.workflow_definition) : null),
    [run],
  );
  const currentDefinition = current?.workflow_definition;
  const currentVersion = current?.version;
  // One object per change, so a memo keyed on the whole run does not recompute on every render.
  return useMemo(
    () => ({
      ranLabels,
      earlierVersion,
      unknownLabels,
      sharedRunId: storedGone ? null : sharedRunId,
      runId: storedGone ? undefined : runId,
      run: storedGone ? undefined : run,
      blocksByLabel: projection?.blocks ?? EMPTY,
      loopsByLabel: projection?.loops ?? NO_LOOPS,
      runsByLabel: projection?.runs ?? NO_RUNS,
      live: Boolean(run && runIsLogicallyActive(run)),
      // Until the run's step rows arrive, a missing row means "not loaded", not "not reached".
      rowsLoaded: Boolean(projection),
      rowsSettled: projection?.rowsSettled ?? false,
      // Whether this browser started the run (or opened it by URL). Only an owned run locks the Doc,
      // offers Stop and ends with a toast; a run from elsewhere is drawn the same but read-only.
      owned: Boolean(urlRunId || (runId && runId === storedRunId)),
      blockRun,
      blockRunTarget,
      currentDefinition,
      currentVersion,
      proposalPending,
      previous,
    }),
    [
      ranLabels,
      earlierVersion,
      unknownLabels,
      storedGone,
      sharedRunId,
      runId,
      run,
      projection,
      urlRunId,
      storedRunId,
      blockRun,
      blockRunTarget,
      currentDefinition,
      currentVersion,
      proposalPending,
      previous,
    ],
  );
}
