import { useCallback, useEffect, useRef, useState } from "react";

import { getClient } from "@/api/AxiosClient";
import {
  artifactIdFromContentUrl,
  expiryFromSignedUrl,
  mintSignedArtifactUrl,
  refreshDelayMs,
} from "@/api/artifactUrls";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

type Props = Omit<React.ComponentPropsWithoutRef<"video">, "src"> & {
  src: string;
};

/**
 * Drop-in `<video>` for Skyvern recording artifacts. Signed content URLs
 * expire; long playback sessions issue Range requests past that boundary and
 * would 403 (SKY-12541). This swaps in a freshly minted URL shortly before
 * expiry — and once on error as a backstop — preserving playback position.
 * Non-Skyvern URLs (storage presigned, file://) render as a plain video.
 */
function ArtifactVideo({
  src,
  onError,
  onLoadedMetadata,
  ...videoProps
}: Props) {
  const credentialGetter = useCredentialGetter();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const resumeRef = useRef<{ time: number; playing: boolean } | null>(null);
  const retriedOnErrorRef = useRef(false);
  const [url, setUrl] = useState(src);

  useEffect(() => {
    setUrl(src);
    resumeRef.current = null;
    retriedOnErrorRef.current = false;
  }, [src]);

  const artifactId = artifactIdFromContentUrl(src);

  const refresh = useCallback(async () => {
    if (!artifactId) {
      return;
    }
    const video = videoRef.current;
    if (video) {
      resumeRef.current = {
        time: video.currentTime,
        playing: !video.paused && !video.ended,
      };
    }
    try {
      const client = await getClient(credentialGetter);
      const minted = await mintSignedArtifactUrl(client, artifactId);
      setUrl(minted.signed_url);
    } catch {
      // Leave the current URL in place; the on-error backstop (or the next
      // timer tick) retries.
    }
  }, [artifactId, credentialGetter]);

  useEffect(() => {
    const expiresAt = expiryFromSignedUrl(url);
    if (!artifactId || expiresAt === null) {
      return;
    }
    const delayMs = refreshDelayMs(expiresAt, Date.now());
    // setTimeout overflows int32 and fires immediately past ~24.8 days.
    if (delayMs > 2 ** 31 - 1) {
      return;
    }
    const timer = window.setTimeout(() => {
      void refresh();
    }, delayMs);
    return () => window.clearTimeout(timer);
  }, [artifactId, url, refresh]);

  const handleLoadedMetadata = (
    event: React.SyntheticEvent<HTMLVideoElement>,
  ) => {
    const resume = resumeRef.current;
    if (resume) {
      resumeRef.current = null;
      event.currentTarget.currentTime = resume.time;
      if (resume.playing) {
        void event.currentTarget.play().catch(() => {});
      }
    }
    onLoadedMetadata?.(event);
  };

  const handleError = (event: React.SyntheticEvent<HTMLVideoElement>) => {
    if (artifactId && !retriedOnErrorRef.current) {
      retriedOnErrorRef.current = true;
      void refresh();
    }
    onError?.(event);
  };

  return (
    <video
      ref={videoRef}
      src={url}
      onLoadedMetadata={handleLoadedMetadata}
      onError={handleError}
      {...videoProps}
    />
  );
}

export { ArtifactVideo };
