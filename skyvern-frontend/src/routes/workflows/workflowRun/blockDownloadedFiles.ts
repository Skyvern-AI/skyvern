// The workflow-run-level downloaded_file_urls is regenerated with fresh
// presigned URLs on every API fetch, while a block's output.downloaded_file_urls
// was persisted at execution time and will expire after PRESIGNED_URL_EXPIRATION.
// For runs older than that TTL we swap each block URL for the matching run-level
// one (keyed by the URL path before the signed query string), falling back to
// the original if no match is found.
function urlKey(url: string): string {
  return url.split("?")[0] ?? url;
}

/**
 * Best-effort filename for a downloaded-file URL.
 *
 * Two URL shapes are in the wild:
 *   1. Short signed artifact URLs introduced in SKY-8861, of shape
 *      ``/v1/artifacts/{id}/content?artifact_name=foo.pdf&...``. The path
 *      basename is always ``content``; the real filename is in the query
 *      parameter.
 *   2. Legacy S3 presigned URLs, of shape
 *      ``https://skyvern-uploads.s3.amazonaws.com/downloads/.../foo.pdf?...``.
 *      The path basename *is* the filename, but it may be percent-encoded
 *      for filenames containing spaces / unicode.
 */
function filenameForDownloadedFileUrl(url: string): string {
  const fallback = "download";
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return fallback;
  }
  // searchParams.get auto-decodes percent-encoding, so non-ASCII
  // artifact_name values round-trip without an explicit decodeURIComponent.
  const fromQuery = parsed.searchParams.get("artifact_name");
  if (fromQuery) {
    return fromQuery;
  }
  const last = parsed.pathname.split("/").pop();
  if (!last || last === "content") {
    return fallback;
  }
  try {
    return decodeURIComponent(last);
  } catch {
    return last;
  }
}

function getBlockDownloadedFileUrls(
  blockOutput: object | Array<unknown> | string | null | undefined,
  freshFallbackUrls: ReadonlyArray<string>,
): Array<string> {
  if (
    !blockOutput ||
    typeof blockOutput !== "object" ||
    Array.isArray(blockOutput) ||
    !("downloaded_file_urls" in blockOutput) ||
    !Array.isArray(
      (blockOutput as Record<string, unknown>).downloaded_file_urls,
    )
  ) {
    return [];
  }

  const blockUrls = (
    (blockOutput as Record<string, unknown>)
      .downloaded_file_urls as Array<unknown>
  ).filter((url): url is string => typeof url === "string");

  if (blockUrls.length === 0) {
    return [];
  }

  const freshByPath = new Map<string, string>();
  for (const url of freshFallbackUrls) {
    freshByPath.set(urlKey(url), url);
  }

  return blockUrls.map((url) => freshByPath.get(urlKey(url)) ?? url);
}

export { filenameForDownloadedFileUrl, getBlockDownloadedFileUrls };
