import { ArtifactApiResponse, TaskApiResponse } from "@/api/types";
import { artifactApiBaseUrl } from "@/util/env";

export function getImageURL(artifact: ArtifactApiResponse): string {
  if (artifact.signed_url) {
    return artifact.signed_url;
  }
  if (artifact.uri.startsWith("file://")) {
    return `${artifactApiBaseUrl}/artifact/image?path=${artifact.uri.slice(7)}`;
  }
  return artifact.uri;
}

export function getScreenshotURL(task: TaskApiResponse) {
  if (!task.screenshot_url) {
    return;
  }
  if (task.screenshot_url?.startsWith("file://")) {
    return `${artifactApiBaseUrl}/artifact/image?path=${task.screenshot_url.slice(7)}`;
  }
  return task.screenshot_url;
}

export function getRecordingURL(task: TaskApiResponse) {
  if (!task.recording_url) {
    return null;
  }
  if (task.recording_url?.startsWith("file://")) {
    return `${artifactApiBaseUrl}/artifact/recording?path=${task.recording_url.slice(7)}`;
  }
  return task.recording_url;
}
