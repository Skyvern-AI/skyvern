import { useCallback, useEffect, useRef, useState } from "react";

import { mintSignedArtifactUrl } from "@/api/artifactUrls";
import { ArtifactApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getImageURL } from "@/routes/tasks/detail/artifactUtils";

/**
 * Image source for an artifact that survives signed-URL expiry (SKY-12541):
 * on image error, mint a fresh short-lived URL once and retry before giving
 * up. `imageFailed` only turns true after the minted retry also fails.
 */
function useArtifactImageSrc(artifact: ArtifactApiResponse | undefined) {
  const credentialGetter = useCredentialGetter();
  const [minted, setMinted] = useState<{ key: string; url: string } | null>(
    null,
  );
  const [failedKey, setFailedKey] = useState<string | null>(null);

  const artifactId = artifact?.artifact_id;
  const baseSrc = artifact ? getImageURL(artifact) : undefined;
  // file://-backed sources render through the local /artifact/image proxy and
  // never expire, so an error there is a real failure. Everything else (signed
  // content URLs, storage-presigned URLs) self-heals by re-minting from the id.
  const mintable = !(artifact?.signed_url ?? artifact?.uri)?.startsWith(
    "file://",
  );
  const currentArtifactIdRef = useRef(artifactId);
  // Keyed so a failure or minted URL from the previous artifact is not used on the first render after a switch.
  const imageKey = `${artifactId}|${baseSrc}`;
  const imageFailed = failedKey === imageKey;
  const mintedUrl = minted?.key === imageKey ? minted.url : null;

  useEffect(() => {
    currentArtifactIdRef.current = artifactId;
    setMinted(null);
    setFailedKey(null);
  }, [artifactId, baseSrc]);

  const onImageError = useCallback(() => {
    if (!artifactId || !mintable || mintedUrl) {
      setFailedKey(imageKey);
      return;
    }
    mintSignedArtifactUrl(credentialGetter, artifactId)
      .then((response) => {
        // Ignore mints that resolve after the rendered artifact changed.
        if (currentArtifactIdRef.current !== artifactId) {
          return;
        }
        // Re-rendering the URL that just failed would not fire onError again, so it would never fail over.
        if (response.signed_url === baseSrc) {
          setFailedKey(imageKey);
        } else {
          setMinted({ key: imageKey, url: response.signed_url });
        }
      })
      .catch(() => {
        if (currentArtifactIdRef.current === artifactId) {
          setFailedKey(imageKey);
        }
      });
  }, [artifactId, mintable, mintedUrl, credentialGetter, imageKey, baseSrc]);

  return { src: mintedUrl ?? baseSrc, onImageError, imageFailed };
}

export { useArtifactImageSrc };
