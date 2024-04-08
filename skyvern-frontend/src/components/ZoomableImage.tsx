import Zoom from "react-medium-image-zoom";
import { AspectRatio } from "@/components/ui/aspect-ratio";

type HTMLImageElementProps = React.ComponentProps<"img">;

function ZoomableImage(props: HTMLImageElementProps) {
  return (
    <Zoom>
      <AspectRatio ratio={16 / 9}>
        <img {...props} />
      </AspectRatio>
    </Zoom>
  );
}

export { ZoomableImage };
