// The workflow-run-level downloaded_file_urls is regenerated with fresh
// presigned URLs on every API fetch, while a block's output.downloaded_file_urls
// was persisted at execution time and will expire after PRESIGNED_URL_EXPIRATION.
// For runs older than that TTL we swap each block URL for the matching run-level
// one (keyed by the URL path before the signed query string), falling back to
// the original if no match is found.
function urlKey(url: string): string {
  return url.split("?")[0] ?? url;
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

export { getBlockDownloadedFileUrls };
