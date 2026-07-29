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
  const [mintedUrl, setMintedUrl] = useState<string | null>(null);
  const [imageFailed, setImageFailed] = useState(false);

  const artifactId = artifact?.artifact_id;
  const baseSrc = artifact ? getImageURL(artifact) : undefined;
  // file://-backed sources render through the local /artifact/image proxy and
  // never expire, so an error there is a real failure. Everything else (signed
  // content URLs, storage-presigned URLs) self-heals by re-minting from the id.
  const mintable = !(artifact?.signed_url ?? artifact?.uri)?.startsWith(
    "file://",
  );
  const currentArtifactIdRef = useRef(artifactId);

  useEffect(() => {
    currentArtifactIdRef.current = artifactId;
    setMintedUrl(null);
    setImageFailed(false);
  }, [artifactId, baseSrc]);

  const onImageError = useCallback(() => {
    if (!artifactId || !mintable || mintedUrl) {
      setImageFailed(true);
      return;
    }
    mintSignedArtifactUrl(credentialGetter, artifactId)
      .then((minted) => {
        // Ignore mints that resolve after the rendered artifact changed.
        if (currentArtifactIdRef.current === artifactId) {
          setMintedUrl(minted.signed_url);
        }
      })
      .catch(() => {
        if (currentArtifactIdRef.current === artifactId) {
          setImageFailed(true);
        }
      });
  }, [artifactId, mintable, mintedUrl, credentialGetter]);

  return { src: mintedUrl ?? baseSrc, onImageError, imageFailed };
}

export { useArtifactImageSrc };
