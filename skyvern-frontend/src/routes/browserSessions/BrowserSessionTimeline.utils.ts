import { normalizeUtcTimestamp } from "@/util/timeFormat";

import { areRecordingsIncomplete } from "./browserSessionQueryUtils";

type SessionTimelineKind = "download" | "recording";

type TimelineArtifactSource = {
  checksum?: string | null;
  filename?: string | null;
  modified_at?: string | null;
  url?: string | null;
};

type SessionTimelineItem = {
  checksum: string | null;
  filename: string;
  kind: SessionTimelineKind;
  modified_at: string | null;
  url: string | null;
};

type BuildSessionTimelineOptions = {
  downloadedFiles?: readonly TimelineArtifactSource[] | null;
  recordings?: readonly TimelineArtifactSource[] | null;
  status?: string | null;
};

const sessionTimelineKindLabels: Record<SessionTimelineKind, string> = {
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

function buildSessionTimeline({
  downloadedFiles,
  recordings,
  status,
}: BuildSessionTimelineOptions): SessionTimelineItem[] {
  const items: SessionTimelineItem[] = [
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

  return items.sort(
    (left, right) =>
      timelineTimestamp(right.modified_at) -
        timelineTimestamp(left.modified_at) ||
      left.kind.localeCompare(right.kind) ||
      left.filename.localeCompare(right.filename),
  );
}

export { buildSessionTimeline, getSessionTimelineKindLabel };
