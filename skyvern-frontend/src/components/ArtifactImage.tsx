import { ArtifactApiResponse } from "@/api/types";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useArtifactImageSrc } from "@/hooks/useArtifactImageSrc";

type Props = Omit<
  React.ComponentProps<typeof ZoomableImage>,
  "src" | "onError"
> & {
  artifact: ArtifactApiResponse;
};

/** ZoomableImage for an artifact; re-mints its URL once on load error (SKY-12541). */
function ArtifactImage({ artifact, ...imageProps }: Props) {
  const { src, onImageError } = useArtifactImageSrc(artifact);
  return <ZoomableImage src={src} onError={onImageError} {...imageProps} />;
}

export { ArtifactImage };
