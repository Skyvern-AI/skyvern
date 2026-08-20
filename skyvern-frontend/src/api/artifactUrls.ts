import axios from "axios";

import { CredentialGetter, getClient } from "@/api/AxiosClient";

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
  credentialGetter: CredentialGetter | null,
  artifactId: string,
): Promise<ArtifactSignedUrlApiResponse> {
  // The signed-url route is registered on the `/v1` router only; the default
  // client's `/api/v1` base 404s.
  const client = await getClient(credentialGetter, "sans-api-v1");
  const response = await client.get<ArtifactSignedUrlApiResponse>(
    `/artifacts/${artifactId}/signed-url`,
  );
  return response.data;
}

/**
 * Freshly minted URL for a Skyvern content URL, at the point of use. Returns
 * the input unchanged for non-artifact URLs or when minting fails (the caller
 * falls back to whatever validity the original URL still has).
 */
export async function freshArtifactUrl(
  credentialGetter: CredentialGetter | null,
  url: string,
): Promise<string> {
  const artifactId = artifactIdFromContentUrl(url);
  if (!artifactId) {
    return url;
  }
  try {
    const minted = await mintSignedArtifactUrl(credentialGetter, artifactId);
    return minted.signed_url;
  } catch {
    return url;
  }
}

/**
 * GET `url`, retrying once on a freshly minted URL when the fetch fails.
 * Deliberately retries on any failure, not just 403: expiry surfaces as an
 * opaque error on cross-origin fetches, and the mint only runs on the error
 * path.
 */
export async function getWithMintRetry(
  url: string,
  artifactId: string,
  credentialGetter: CredentialGetter | null,
): Promise<unknown> {
  try {
    const response = await axios.get(url);
    return response.data;
  } catch (initialError) {
    let minted: ArtifactSignedUrlApiResponse;
    try {
      minted = await mintSignedArtifactUrl(credentialGetter, artifactId);
    } catch {
      throw initialError;
    }
    const response = await axios.get(minted.signed_url);
    return response.data;
  }
}
