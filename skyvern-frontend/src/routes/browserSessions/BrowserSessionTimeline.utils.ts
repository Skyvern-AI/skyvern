import { normalizeUtcTimestamp } from "@/util/timeFormat";

import { areRecordingsIncomplete } from "./browserSessionQueryUtils";

type ActionLogEvent = {
  schema_version: 1;
  event_id: string;
  tool: string;
  selector: string | null;
  value: string | null;
  source_url: string | null;
  occurred_at: string;
  timing_ms: Record<string, number>;
  outcome: "success" | "error";
  error_code: string | null;
  index: number;
  artifact_ref: string | null;
};

type ActionLogPage = {
  events: ActionLogEvent[];
  next_cursor: string | null;
};

type SessionTimelineKind = "action" | "download" | "recording";

type TimelineArtifactSource = {
  checksum?: string | null;
  filename?: string | null;
  modified_at?: string | null;
  url?: string | null;
};

type SessionTimelineArtifactItem = {
  checksum: string | null;
  filename: string;
  kind: "download" | "recording";
  modified_at: string | null;
  url: string | null;
};

type SessionTimelineActionItem = Pick<
  ActionLogEvent,
  "error_code" | "event_id" | "index" | "outcome" | "timing_ms" | "tool"
> & {
  kind: "action";
  occurred_at: string | null;
};

type SessionTimelineItem =
  | SessionTimelineActionItem
  | SessionTimelineArtifactItem;

type BuildSessionTimelineOptions = {
  actionEvents?: readonly ActionLogEvent[] | null;
  downloadedFiles?: readonly TimelineArtifactSource[] | null;
  recordings?: readonly TimelineArtifactSource[] | null;
  status?: string | null;
};

const sessionTimelineKindLabels: Record<SessionTimelineKind, string> = {
  action: "Action",
  download: "Download",
  recording: "Recording",
};

function getSessionTimelineKindLabel(kind: string): string {
  return sessionTimelineKindLabels[kind as SessionTimelineKind] ?? "Artifact";
}

function timelineFilename(
  artifact: TimelineArtifactSource,
  kind: SessionTimelineKind,
  index: number,
): string {
  const urlPath = artifact.url?.split("?")[0];
  return (
    artifact.filename ||
    urlPath?.split("/").pop() ||
    `${getSessionTimelineKindLabel(kind)} ${index + 1}`
  );
}

function timelineTimestamp(modifiedAt: string | null): number {
  const timestamp = modifiedAt
    ? Date.parse(normalizeUtcTimestamp(modifiedAt))
    : Number.NaN;
  return Number.isNaN(timestamp) ? Number.NEGATIVE_INFINITY : timestamp;
}

function validTimestamp(modifiedAt: string | null | undefined): string | null {
  if (!modifiedAt) {
    return null;
  }
  return Number.isNaN(Date.parse(normalizeUtcTimestamp(modifiedAt)))
    ? null
    : modifiedAt;
}

function mergeActionLogEvents(
  current: readonly ActionLogEvent[],
  incoming: readonly ActionLogEvent[],
): ActionLogEvent[] {
  const eventsById = new Map(current.map((event) => [event.event_id, event]));
  incoming.forEach((event) => eventsById.set(event.event_id, event));
  return [...eventsById.values()];
}

function getActionDurationMs(timingMs: Readonly<Record<string, number>>) {
  const durations = Object.values(timingMs);
  return timingMs.total ?? (durations.length ? Math.max(...durations) : null);
}

function timelineItemTimestamp(item: SessionTimelineItem): number {
  return timelineTimestamp(
    item.kind === "action" ? item.occurred_at : item.modified_at,
  );
}

function compareTimelineItems(
  left: SessionTimelineItem,
  right: SessionTimelineItem,
): number {
  const timeOrder = timelineItemTimestamp(right) - timelineItemTimestamp(left);
  if (timeOrder) {
    return timeOrder;
  }
  if (left.kind === "action" && right.kind === "action") {
    return (
      right.index - left.index || left.event_id.localeCompare(right.event_id)
    );
  }
  if (left.kind === "action") {
    return -1;
  }
  if (right.kind === "action") {
    return 1;
  }
  return (
    left.kind.localeCompare(right.kind) ||
    left.filename.localeCompare(right.filename)
  );
}

function buildSessionTimeline({
  actionEvents,
  downloadedFiles,
  recordings,
  status,
}: BuildSessionTimelineOptions): SessionTimelineItem[] {
  const items: SessionTimelineItem[] = [
    ...mergeActionLogEvents([], actionEvents ?? []).map((event) => ({
      error_code: event.error_code,
      event_id: event.event_id,
      index: event.index,
      kind: "action" as const,
      occurred_at: validTimestamp(event.occurred_at),
      outcome: event.outcome,
      timing_ms: event.timing_ms,
      tool: event.tool,
    })),
    ...(downloadedFiles ?? []).map((file, index) => ({
      checksum: file.checksum ?? null,
      filename: timelineFilename(file, "download", index),
      kind: "download" as const,
      modified_at: validTimestamp(file.modified_at),
      url: file.url ?? null,
    })),
    ...(areRecordingsIncomplete(status) ? [] : (recordings ?? [])).map(
      (recording, index) => ({
        checksum: recording.checksum ?? null,
        filename: timelineFilename(recording, "recording", index),
        kind: "recording" as const,
        modified_at: validTimestamp(recording.modified_at),
        url: recording.url ?? null,
      }),
    ),
  ];

  return items.sort(compareTimelineItems);
}

export {
  type ActionLogEvent,
  type ActionLogPage,
  type SessionTimelineItem,
  buildSessionTimeline,
  getActionDurationMs,
  getSessionTimelineKindLabel,
  mergeActionLogEvents,
};
