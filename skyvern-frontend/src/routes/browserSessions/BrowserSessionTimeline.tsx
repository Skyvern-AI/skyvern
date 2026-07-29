import {
  ActivityLogIcon,
  DownloadIcon,
  ExternalLinkIcon,
  FileIcon,
  VideoIcon,
} from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { getClient } from "@/api/AxiosClient";
import { ArtifactDownloadLink } from "@/components/ArtifactDownloadLink";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { type BrowserSession } from "@/routes/workflows/types/browserSessionTypes";
import { basicLocalTimeFormat } from "@/util/timeFormat";

import {
  type ActionLogEvent,
  type ActionLogPage,
  buildSessionTimeline,
  getActionDurationMs,
  getSessionTimelineKindLabel,
  mergeActionLogEvents,
} from "./BrowserSessionTimeline.utils";
import { getBrowserSessionRefetchIntervalMs } from "./browserSessionQueryUtils";
import { getRecordingUrl } from "./recordingUrl";

const kindIcons: Record<string, typeof FileIcon> = {
  action: ActivityLogIcon,
  download: DownloadIcon,
  recording: VideoIcon,
};

const ACTION_LOG_POLL_INTERVAL_MS = 1000;
const activeBrowserSessionStatuses = new Set(["created", "retry", "running"]);

type ActionLogState = {
  browserSessionId: string | undefined;
  cursor: string | null;
  events: ActionLogEvent[];
};

function BrowserSessionTimeline() {
  const { browserSessionId } = useParams();
  const credentialGetter = useCredentialGetter();
  const [actionLogState, setActionLogState] = useState<ActionLogState>({
    browserSessionId,
    cursor: null,
    events: [],
  });
  const previousBrowserSession = useRef<{
    browserSessionId: string | undefined;
    status: string | undefined;
  }>();
  const [pendingFinalActionLogFetch, setPendingFinalActionLogFetch] =
    useState(false);
  const actionCursor =
    actionLogState.browserSessionId === browserSessionId
      ? actionLogState.cursor
      : null;
  const actionEvents =
    actionLogState.browserSessionId === browserSessionId
      ? actionLogState.events
      : [];
  const {
    data: browserSession,
    isLoading,
    error,
  } = useQuery<BrowserSession>({
    queryKey: ["browserSession", browserSessionId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.get(
        `/browser_sessions/${browserSessionId}`,
      );
      return response.data;
    },
    enabled: !!browserSessionId,
    refetchInterval: (query) =>
      getBrowserSessionRefetchIntervalMs(query.state.data),
  });
  const {
    data: actionLogPage,
    isFetching: isActionLogFetching,
    isLoading: isActionLogLoading,
    refetch: refetchActionLogs,
  } = useQuery<ActionLogPage>({
    queryKey: ["browserSessionActionLogs", browserSessionId, actionCursor],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.get<ActionLogPage>(
        `/browser_sessions/${browserSessionId}/action_logs`,
        {
          params: actionCursor === null ? undefined : { cursor: actionCursor },
        },
      );
      return response.data;
    },
    enabled: !!browserSessionId,
    refetchInterval: (query) => {
      if (
        isAxiosError(query.state.error) &&
        query.state.error.response?.status === 404
      ) {
        return false;
      }
      return !browserSession?.status ||
        activeBrowserSessionStatuses.has(browserSession.status)
        ? ACTION_LOG_POLL_INTERVAL_MS
        : false;
    },
  });

  useEffect(() => {
    const previous = previousBrowserSession.current;
    const status = browserSession?.status;
    if (
      previous !== undefined &&
      previous.browserSessionId === browserSessionId &&
      previous.status &&
      activeBrowserSessionStatuses.has(previous.status) &&
      status &&
      !activeBrowserSessionStatuses.has(status)
    ) {
      setPendingFinalActionLogFetch(true);
    }
    previousBrowserSession.current = { browserSessionId, status };
  }, [browserSession?.status, browserSessionId]);

  useEffect(() => {
    if (!pendingFinalActionLogFetch || isActionLogFetching) {
      return;
    }
    setPendingFinalActionLogFetch(false);
    void refetchActionLogs();
  }, [pendingFinalActionLogFetch, isActionLogFetching, refetchActionLogs]);

  useEffect(() => {
    if (!actionLogPage || !browserSessionId) {
      return;
    }
    setActionLogState((current) => {
      const sameSession = current.browserSessionId === browserSessionId;
      return {
        browserSessionId,
        cursor:
          actionLogPage.next_cursor ?? (sameSession ? current.cursor : null),
        events: mergeActionLogEvents(
          sameSession ? current.events : [],
          actionLogPage.events,
        ),
      };
    });
  }, [actionLogPage, browserSessionId]);

  const timeline = buildSessionTimeline({
    actionEvents,
    downloadedFiles: browserSession?.downloaded_files,
    recordings: browserSession?.recordings,
    status: browserSession?.status,
  });

  if (isLoading || (isActionLogLoading && actionEvents.length === 0)) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="text-lg">Loading artifacts...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="text-lg text-red-500">
          Error loading timeline: {error.message}
        </div>
      </div>
    );
  }

  if (timeline.length === 0) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="max-w-lg text-center text-lg text-gray-500">
          No timeline events available yet — actions, downloads, and recordings
          will appear here as they become available.
        </div>
      </div>
    );
  }

  return (
    <div className="h-full w-full overflow-auto p-4">
      <div className="mb-4">
        <h2 className="text-xl font-semibold">Artifact Timeline</h2>
        <p className="text-sm text-gray-500">
          Browser actions and when session artifacts became available
        </p>
      </div>

      <ul className="space-y-2">
        {timeline.map((artifact, index) => {
          const KindIcon = kindIcons[artifact.kind] ?? FileIcon;
          if (artifact.kind === "action") {
            const durationMs = getActionDurationMs(artifact.timing_ms);
            return (
              <li
                key={`action-${artifact.event_id}`}
                className="flex items-center gap-3 rounded-lg border p-3"
              >
                <KindIcon className="size-5 shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                      {getSessionTimelineKindLabel(artifact.kind)}
                    </span>
                    <span
                      className={
                        artifact.outcome === "success"
                          ? "text-xs text-green-600"
                          : "text-xs text-red-600"
                      }
                      title={artifact.error_code ?? undefined}
                    >
                      {artifact.outcome}
                    </span>
                    <span className="truncate text-sm" title={artifact.tool}>
                      {artifact.tool}
                    </span>
                  </div>
                  <div className="mt-1 flex gap-2 text-xs text-gray-500">
                    <span>
                      {durationMs === null
                        ? "Duration unavailable"
                        : `${durationMs} ms`}
                    </span>
                    <span>
                      {artifact.occurred_at
                        ? basicLocalTimeFormat(artifact.occurred_at)
                        : "Event time unavailable"}
                    </span>
                  </div>
                </div>
              </li>
            );
          }
          const href =
            artifact.kind === "recording"
              ? getRecordingUrl(artifact.url)
              : artifact.url;

          return (
            <li
              key={`${artifact.kind}-${artifact.checksum ?? artifact.url ?? artifact.filename}-${index}`}
              className="flex items-center gap-3 rounded-lg border p-3"
            >
              <KindIcon className="size-5 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                    {getSessionTimelineKindLabel(artifact.kind)}
                  </span>
                  <span className="truncate text-sm" title={artifact.filename}>
                    {artifact.filename}
                  </span>
                </div>
                <div className="mt-1 text-xs text-gray-500">
                  {artifact.modified_at
                    ? basicLocalTimeFormat(artifact.modified_at)
                    : "Availability time unavailable"}
                </div>
              </div>
              {href && (
                <ArtifactDownloadLink
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={`Open ${artifact.filename}`}
                  className="flex shrink-0 items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-muted"
                >
                  <ExternalLinkIcon className="size-3" />
                  Open
                </ArtifactDownloadLink>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export { BrowserSessionTimeline };
