import { useEffect, useRef } from "react";
import { useSearchParams } from "react-router-dom";

import { Status, WorkflowRunStatusApiResponse } from "@/api/types";
import { statusIsAFailureType, statusIsFinalized } from "@/routes/tasks/types";
import { formatElapsedSeconds, isRecord } from "@/util/utils";

import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import type { ChatMessage } from "./WorkflowCopilotChat";

type RunLifecycleMessage = ChatMessage & { kind: "run_lifecycle" };

const START_JOIN_THRESHOLD_MS = 15_000;
const FAILURE_REASON_MAX_CHARS = 200;

function elapsedMs(startIso: string | null, endIso: string | null): number {
  const start = startIso ? Date.parse(startIso) : NaN;
  const end = endIso ? Date.parse(endIso) : NaN;
  if (Number.isNaN(start) || Number.isNaN(end)) {
    return 0;
  }
  return end - start;
}

function extractedCount(
  outputs: Record<string, unknown> | null,
): number | undefined {
  if (!outputs || !("extracted_information" in outputs)) {
    return undefined;
  }
  const info = outputs.extracted_information;
  if (Array.isArray(info)) {
    return info.length;
  }
  if (isRecord(info)) {
    const arrays = Object.values(info).filter(Array.isArray);
    return arrays.length === 1 ? arrays[0]?.length : undefined;
  }
  return undefined;
}

function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

function failureVerb(status: Status): string {
  if (status === Status.TimedOut) return "timed out";
  if (status === Status.Terminated) return "was terminated";
  return "failed";
}

function buildStartMessage(
  id: string,
  data: WorkflowRunStatusApiResponse,
  isBlockRun: boolean,
): RunLifecycleMessage {
  const age = data.started_at ? Date.now() - Date.parse(data.started_at) : 0;
  const joined = age > START_JOIN_THRESHOLD_MS;
  const content = isBlockRun
    ? joined
      ? "Block run in progress…"
      : "Block run started — watching it now."
    : joined
      ? "Run in progress — watching it now."
      : "Run started — watching it now.";
  return {
    id: `run-lifecycle-${id}-start`,
    sender: "ai",
    kind: "run_lifecycle",
    content,
  };
}

function buildTerminalMessage(
  id: string,
  data: WorkflowRunStatusApiResponse,
  isBlockRun: boolean,
): RunLifecycleMessage {
  const runLabel = isBlockRun ? "Block run" : "Run";
  const dur = formatElapsedSeconds(
    elapsedMs(data.started_at ?? data.created_at, data.finished_at),
  );
  let content: string;
  if (data.status === Status.Canceled) {
    content = `${runLabel} canceled.`;
  } else if (statusIsAFailureType({ status: data.status })) {
    const reason = data.failure_reason
      ? ` — ${truncate(data.failure_reason, FAILURE_REASON_MAX_CHARS)}`
      : "";
    content = `${runLabel} ${failureVerb(data.status)} after ${dur}${reason}. Ask me to diagnose and fix it.`;
  } else {
    const count = extractedCount(data.outputs);
    const extracted = count ? ` — extracted ${count} item(s)` : "";
    content = `${runLabel} completed in ${dur}${extracted}. Want to review or change anything?`;
  }
  return {
    id: `run-lifecycle-${id}-terminal`,
    sender: "ai",
    kind: "run_lifecycle",
    content,
  };
}

type SeenEntry = { start: boolean; terminal: boolean; wasRunning: boolean };

/**
 * Watches the studio's focused run and appends presentational lifecycle lines
 * into the chat's own message list. Never persisted, never sent to the LLM —
 * grounding for the LLM already travels as workflow_run_id on each turn.
 */
export function useRunLifecycleAnnouncements({
  workflowRunId,
  turnInFlight,
  turnOwnedRunIds,
  announce,
}: {
  workflowRunId: string | undefined;
  turnInFlight: boolean;
  // Run ids the copilot claimed via run_outcome this session — the turn already
  // narrates these through its own SSE stream, so we suppress them by identity
  // rather than by the timing of when they were first seen.
  turnOwnedRunIds: { current: Set<string> };
  announce: (message: RunLifecycleMessage) => void;
}): void {
  // enabled: false (not just an omitted workflowRunId) stops useWorkflowRunQuery
  // from falling back to the route's own :workflowRunId and polling a run this
  // chat renders no line for.
  const { data } = useWorkflowRunQuery({
    workflowRunId,
    enabled: workflowRunId !== undefined,
  });
  const seen = useRef(new Map<string, SeenEntry>());
  const [searchParams] = useSearchParams();
  const isBlockRun = searchParams.get("bl") !== null;

  useEffect(() => {
    // Disabling the query above doesn't clear data left over from a prior
    // enabled fetch, so still gate announcing on our own current input.
    if (!workflowRunId || !data) {
      return;
    }
    const id = data.workflow_run_id;
    let entry = seen.current.get(id);
    if (!entry) {
      entry = { start: false, terminal: false, wasRunning: false };
      seen.current.set(id, entry);
    }
    // The copilot's own build/test run narrates itself through the turn's SSE
    // stream, so identity — not timing — is what silences it: a run whose id
    // the turn claimed via run_outcome stays fully silent, forever.
    if (turnOwnedRunIds.current.has(id)) {
      entry.start = true;
      entry.terminal = true;
      return;
    }
    const finalized = statusIsFinalized({ status: data.status });
    // A not-yet-claimed run seen mid-turn may still turn out to be the turn's
    // own (its run_outcome hasn't landed yet), so hold its lines until the turn
    // ends instead of leaking a start line the turn would also narrate. Record
    // that it was live so a completion observed right after the turn ends is
    // announced as a real terminal, not silently absorbed as an
    // already-finished first sight (which is what a genuinely unrelated run
    // finishing inside the post-turn poll gap would otherwise look like).
    if (!entry.start && turnInFlight) {
      if (!finalized) {
        entry.wasRunning = true;
      }
      return;
    }
    if (!entry.start) {
      entry.start = true;
      if (finalized) {
        entry.terminal = true;
        if (entry.wasRunning) {
          announce(buildTerminalMessage(id, data, isBlockRun));
        }
        return;
      }
      announce(buildStartMessage(id, data, isBlockRun));
      return;
    }
    if (!entry.terminal && finalized) {
      entry.terminal = true;
      announce(buildTerminalMessage(id, data, isBlockRun));
    }
  }, [
    workflowRunId,
    data,
    announce,
    isBlockRun,
    turnInFlight,
    turnOwnedRunIds,
  ]);
}
