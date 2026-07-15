import { AxiosInstance } from "axios";

export type ArtifactSignedUrlApiResponse = {
  artifact_id: string;
  signed_url: string;
  expires_at: number | null;
};

const ARTIFACT_CONTENT_PATH = /\/artifacts\/([^/]+)\/content\/?$/;

/** Seconds before a signed URL's expiry at which consumers should re-mint. */
export const REFRESH_MARGIN_SECONDS = 60;

/**
 * Artifact id of a Skyvern signed content URL
 * (`.../artifacts/{id}/content?...`), or null for anything else (storage
 * presigned URLs, file:// paths) — those can't be re-minted by id.
 */
export function artifactIdFromContentUrl(
  url: string | null | undefined,
): string | null {
  if (!url) {
    return null;
  }
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const match = ARTIFACT_CONTENT_PATH.exec(parsed.pathname);
  return match?.[1] ?? null;
}

/** Unix-seconds expiry of a signed URL, read from its `expiry` query param. */
export function expiryFromSignedUrl(
  url: string | null | undefined,
): number | null {
  if (!url) {
    return null;
  }
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const expiry = parsed.searchParams.get("expiry");
  if (!expiry || !/^\d+$/.test(expiry)) {
    return null;
  }
  return Number(expiry);
}

/** Milliseconds until a URL expiring at `expiresAtSeconds` should be re-minted. */
export function refreshDelayMs(
  expiresAtSeconds: number,
  nowMs: number,
): number {
  return Math.max(
    0,
    (expiresAtSeconds - REFRESH_MARGIN_SECONDS) * 1000 - nowMs,
  );
}

/** Mint a fresh short-lived content URL for the artifact (SKY-12541). */
export async function mintSignedArtifactUrl(
  client: AxiosInstance,
  artifactId: string,
): Promise<ArtifactSignedUrlApiResponse> {
  const response = await client.get<ArtifactSignedUrlApiResponse>(
    `/artifacts/${artifactId}/signed-url`,
  );
  return response.data;
}
