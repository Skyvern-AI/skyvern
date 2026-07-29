import { artifactApiBaseUrl } from "@/util/env";

function getRecordingUrl(url: string | null | undefined): string | null {
  if (!url) {
    return null;
  }
  if (url.startsWith("file://")) {
    return `${artifactApiBaseUrl}/artifact/recording?path=${encodeURIComponent(url.slice(7))}`;
  }
  return url;
}

export { getRecordingUrl };
