import { useEffect, useState } from "react";

import { artifactIdFromContentUrl } from "@/api/artifactUrls";
import { ArtifactDownloadLink } from "@/components/ArtifactDownloadLink";
import { ArtifactVideo } from "@/components/ArtifactVideo";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  type BrowserSession,
  type Recording,
} from "@/routes/workflows/types/browserSessionTypes";
import { basicLocalTimeFormat } from "@/util/timeFormat";

import {
  areRecordingsIncomplete,
  getBrowserSessionRefetchIntervalMs,
  getPostTerminalRecordingDeadlineMs,
} from "./browserSessionQueryUtils";
import { getRecordingUrl } from "./recordingUrl";

const EMPTY_RECORDINGS: Recording[] = [];

type RecordingOption = {
  identity: string | null;
  label: string;
  recording: Recording;
  value: string;
};

type RecordingSelection = {
  identity: string | null;
  recordings: Recording[];
  value: string;
};

function nonEmpty(value: string | null): string | null {
  return value?.trim() ? value : null;
}

function getRecordingIdentity(recording: Recording): string | null {
  const artifactId = nonEmpty(recording.artifact_id);
  if (artifactId) {
    return `artifact:${artifactId}`;
  }

  const fields = [
    nonEmpty(recording.checksum),
    nonEmpty(recording.filename),
    nonEmpty(recording.modified_at),
  ];
  return fields.some(Boolean) ? `file:${JSON.stringify(fields)}` : null;
}

function getRecordingLabel(recording: Recording, index: number): string {
  if (recording.modified_at) {
    return `Recording — ${basicLocalTimeFormat(recording.modified_at)}`;
  }
  return nonEmpty(recording.filename) ?? `Recording ${index + 1}`;
}

function makeRecordingOptions(recordings: Recording[]): RecordingOption[] {
  const labels = recordings.map(getRecordingLabel);
  const labelCounts = new Map<string, number>();
  for (const label of labels) {
    labelCounts.set(label, (labelCounts.get(label) ?? 0) + 1);
  }

  return recordings.map((recording, index) => {
    const label = labels[index]!;
    return {
      identity: getRecordingIdentity(recording),
      label:
        (labelCounts.get(label) ?? 0) > 1 ? `${label} (${index + 1})` : label,
      recording,
      // This value is intentionally scoped to this response. Selection persistence
      // uses identity, never the response position.
      value: `recording-${index}`,
    };
  });
}

function StateMessage({
  primary,
  supporting,
}: {
  primary: string;
  supporting?: string;
}) {
  return (
    <div className="flex h-full w-full items-center justify-center p-4">
      <div className="text-center">
        <div className="text-lg text-gray-500">{primary}</div>
        {supporting && (
          <div className="mt-1 text-sm text-gray-400">{supporting}</div>
        )}
      </div>
    </div>
  );
}

function useRecordingFinalizationNow(browserSession: BrowserSession): number {
  const [, forceRenderAt] = useState(Date.now);
  const deadline = getPostTerminalRecordingDeadlineMs(browserSession);

  useEffect(() => {
    if (deadline === null) {
      return;
    }
    const timeout = window.setTimeout(
      () => forceRenderAt(Date.now()),
      Math.max(0, deadline - Date.now()),
    );
    return () => window.clearTimeout(timeout);
  }, [deadline]);

  return Date.now();
}

function BrowserSessionVideo({
  browserSession,
  refreshBrowserSession,
}: {
  browserSession: BrowserSession;
  refreshBrowserSession?: () => Promise<BrowserSession | undefined>;
}) {
  const [selection, setSelection] = useState<RecordingSelection | null>(null);
  const now = useRecordingFinalizationNow(browserSession);
  const status = browserSession.status;

  if (status === "created" || status === "retry") {
    return (
      <StateMessage
        primary="Preparing browser"
        supporting="Waiting for the browser session to start..."
      />
    );
  }

  if (areRecordingsIncomplete(status)) {
    return (
      <StateMessage
        primary="Recording in progress"
        supporting="Recordings will be available after the session ends."
      />
    );
  }

  const recordings = browserSession.recordings ?? EMPTY_RECORDINGS;
  if (!recordings.length) {
    return getBrowserSessionRefetchIntervalMs(browserSession, now) !== false ? (
      <StateMessage primary="Recordings are still processing." />
    ) : (
      <StateMessage primary="No recordings were created for this session." />
    );
  }

  const options = makeRecordingOptions(recordings);
  const selectedOption =
    (selection?.recordings === recordings
      ? options.find((option) => option.value === selection.value)
      : selection?.identity
        ? options.find((option) => option.identity === selection.identity)
        : undefined) ?? options[0]!;
  const recordingUrl = getRecordingUrl(selectedOption.recording.url);
  const refreshDownloadHref =
    recordingUrl &&
    !artifactIdFromContentUrl(recordingUrl) &&
    refreshBrowserSession
      ? async () => {
          const refreshedSession = await refreshBrowserSession();
          const selectedIdentity = getRecordingIdentity(
            selectedOption.recording,
          );
          const refreshedRecording = selectedIdentity
            ? (refreshedSession?.recordings?.find(
                (recording) =>
                  getRecordingIdentity(recording) === selectedIdentity,
              ) ?? refreshedSession?.recordings?.[0])
            : refreshedSession?.recordings?.[0];
          return getRecordingUrl(refreshedRecording?.url) ?? recordingUrl;
        }
      : undefined;
  const accessibleName = selectedOption.recording.modified_at
    ? `Recording — ${basicLocalTimeFormat(selectedOption.recording.modified_at)}`
    : "Recording";

  return (
    <div className="h-full w-full overflow-auto p-4">
      <div className="mx-auto grid w-full max-w-4xl gap-4">
        {options.length >= 2 && (
          <div className="grid gap-2">
            <Label htmlFor="recording-selector">Recording</Label>
            <Select
              value={selectedOption.value}
              onValueChange={(value) => {
                const option = options.find((item) => item.value === value);
                if (option) {
                  setSelection({
                    identity: option.identity,
                    recordings,
                    value: option.value,
                  });
                }
              }}
            >
              <SelectTrigger id="recording-selector" className="max-w-4xl">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {options.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}

        {recordingUrl ? (
          <>
            <ArtifactVideo
              aria-label={accessibleName}
              controls
              className="w-full rounded-lg"
              src={recordingUrl}
              preload="metadata"
            >
              Your browser does not support the video tag.
            </ArtifactVideo>
            <div className="flex items-center gap-3 text-xs text-gray-500">
              {selectedOption.recording.modified_at && (
                <span>
                  {basicLocalTimeFormat(selectedOption.recording.modified_at)}
                </span>
              )}
              <ArtifactDownloadLink
                href={recordingUrl}
                refreshHref={refreshDownloadHref}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-600 hover:text-blue-800"
              >
                Download
              </ArtifactDownloadLink>
            </div>
          </>
        ) : (
          <StateMessage primary="This recording is still processing." />
        )}
      </div>
    </div>
  );
}

export { BrowserSessionVideo };
