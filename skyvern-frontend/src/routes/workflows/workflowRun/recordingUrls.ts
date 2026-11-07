import { artifactApiBaseUrl } from "@/util/env";

function resolveRecordingUrl(url: string): string {
  if (url.startsWith("file://")) {
    return `${artifactApiBaseUrl}/artifact/recording?path=${encodeURIComponent(url.slice(7))}`;
  }
  return url;
}

type RecordingSource = {
  recording_urls?: string[] | null;
  recording_url?: string | null;
};

export function getRecordingUrls(
  workflowRun: RecordingSource | null | undefined,
): string[] {
  if (!workflowRun) {
    return [];
  }
  const raw =
    workflowRun.recording_urls && workflowRun.recording_urls.length > 0
      ? workflowRun.recording_urls
      : workflowRun.recording_url
        ? [workflowRun.recording_url]
        : [];
  return raw.map(resolveRecordingUrl);
}
