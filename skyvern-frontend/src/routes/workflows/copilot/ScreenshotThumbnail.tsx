import * as VisuallyHidden from "@radix-ui/react-visually-hidden";
import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse } from "@/api/types";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { useArtifactImageSrc } from "@/hooks/useArtifactImageSrc";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

// The box is sized before the image loads: the chat re-scrolls on state changes, not on image load.
const BOX = "aspect-video w-36 shrink-0 rounded border border-border";

export function ScreenshotThumbnail({ artifactId }: { artifactId: string }) {
  const credentialGetter = useCredentialGetter();
  const { data: artifact, isError } = useQuery<ArtifactApiResponse>({
    queryKey: ["artifact", artifactId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/artifacts/${artifactId}`)
        .then((response) => response.data);
    },
    refetchOnWindowFocus: false,
    staleTime: Infinity,
    retry: 1,
  });
  const { src, onImageError, imageFailed } = useArtifactImageSrc(artifact);

  if (isError || imageFailed) {
    return (
      <div
        className={`${BOX} flex items-center justify-center bg-muted/20 px-2 text-center text-[11px] text-muted-foreground`}
      >
        Screenshot unavailable
      </div>
    );
  }

  return (
    <Dialog>
      <DialogTrigger asChild>
        <button
          type="button"
          aria-label="View screenshot"
          disabled={src === undefined}
          className={`${BOX} cursor-zoom-in overflow-hidden bg-muted/20 hover:border-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default`}
        >
          {src === undefined ? null : (
            <img
              loading="lazy"
              src={src}
              alt=""
              onError={onImageError}
              className="size-full object-cover object-top"
            />
          )}
        </button>
      </DialogTrigger>
      <DialogContent
        aria-describedby={undefined}
        className="w-max max-w-none overflow-hidden p-0 pt-10"
      >
        <VisuallyHidden.Root>
          <DialogTitle>Screenshot</DialogTitle>
        </VisuallyHidden.Root>
        <img
          src={src}
          alt="Browser screenshot"
          onError={onImageError}
          className="max-h-[85vh] max-w-[90vw] object-contain"
        />
      </DialogContent>
    </Dialog>
  );
}
