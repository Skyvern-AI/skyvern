import {
  DownloadIcon,
  ExternalLinkIcon,
  FileIcon,
  VideoIcon,
} from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { getClient } from "@/api/AxiosClient";
import { ArtifactDownloadLink } from "@/components/ArtifactDownloadLink";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { type BrowserSession } from "@/routes/workflows/types/browserSessionTypes";
import { basicLocalTimeFormat } from "@/util/timeFormat";

import {
  buildSessionTimeline,
  getSessionTimelineKindLabel,
} from "./BrowserSessionTimeline.utils";
import { getRecordingUrl } from "./recordingUrl";

const kindIcons: Record<string, typeof FileIcon> = {
  download: DownloadIcon,
  recording: VideoIcon,
};

function BrowserSessionTimeline() {
  const { browserSessionId } = useParams();
  const credentialGetter = useCredentialGetter();
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
  });

  const timeline = buildSessionTimeline({
    downloadedFiles: browserSession?.downloaded_files,
    recordings: browserSession?.recordings,
    status: browserSession?.status,
  });

  if (isLoading) {
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
          Error loading artifacts: {error.message}
        </div>
      </div>
    );
  }

  if (timeline.length === 0) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="max-w-lg text-center text-lg text-gray-500">
          No artifacts available yet — downloads and recordings will appear here
          as they become available.
        </div>
      </div>
    );
  }

  return (
    <div className="h-full w-full overflow-auto p-4">
      <div className="mb-4">
        <h2 className="text-xl font-semibold">Artifact Timeline</h2>
        <p className="text-sm text-gray-500">
          When files from this session became available
        </p>
      </div>

      <ul className="space-y-2">
        {timeline.map((artifact, index) => {
          const KindIcon = kindIcons[artifact.kind] ?? FileIcon;
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
