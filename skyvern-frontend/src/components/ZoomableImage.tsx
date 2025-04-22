import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import * as apiTypes from "@/api/types";

interface ZoomableImageProps extends React.ComponentProps<"img"> {
  captureClick?: (pos: apiTypes.Position) => void;
}

function ZoomableImage({ captureClick, ...props }: ZoomableImageProps) {
  const [modalOpen, setModalOpen] = useState(false);
  const [zoom, setZoom] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null);
  const [position, setPosition] = useState({ x: 0, y: 0 });

  const classes = {
    img: clsx("cursor-zoom-in object-contain", props.className),
    modalCloseButton:
      "absolute right-4 top-4 cursor-pointer text-4xl text-white",
    modalContainer: clsx(
      "fixed inset-0 z-50 flex justify-center overflow-auto bg-black bg-opacity-75 p-16",
      {
        "items-center": !zoom,
        "items-baseline": zoom,
      },
    ),
    modalImg: captureClick
      ? clsx(
          "m-0 h-full max-h-full min-h-full w-full max-w-full object-contain",
        )
      : clsx({
          "m-0 h-full max-h-full min-h-full w-full max-w-full cursor-zoom-in object-contain":
            !zoom,
          "m-0 ml-auto mr-auto max-h-none min-h-full max-w-none cursor-zoom-out object-contain":
            zoom,
        }),
  };

  const openModal = () => {
    setZoom(false);
    setModalOpen(true);
  };

  const closeModal = () => {
    setModalOpen(false);
  };

  const sendClick = () => {
    captureClick?.(position);
  };

  /**
   * The image is shown via `object-fit: contain`, and so the bounding box is
   * incorrect. Have to adjust it. If the styling changes, this function will
   * either no longer be needed, or have to be adjusted.
   */
  const getContainedImageSize = (img: HTMLImageElement) => {
    const containerRect = img.getBoundingClientRect();
    const containerWidth = containerRect.width;
    const containerHeight = containerRect.height;

    const naturalWidth = img.naturalWidth;
    const naturalHeight = img.naturalHeight;

    const containerRatio = containerWidth / containerHeight;
    const imageRatio = naturalWidth / naturalHeight;

    let renderedWidth, renderedHeight;

    if (imageRatio > containerRatio) {
      renderedWidth = containerWidth;
      renderedHeight = containerWidth / imageRatio;
    } else {
      renderedHeight = containerHeight;
      renderedWidth = containerHeight * imageRatio;
    }

    const left = containerRect.left + (containerWidth - renderedWidth) / 2;
    const top = containerRect.top + (containerHeight - renderedHeight) / 2;

    return {
      width: renderedWidth,
      height: renderedHeight,
      left: left,
      top: top,
      right: left + renderedWidth,
      bottom: top + renderedHeight,
    };
  };

  /**
   * NOTE(jdo): These are constant offsets to make the click events happen at
   * the correct location in the running browser. I believe the `y` component
   * is the height of the browser chrome (address bar, buttons, etc.).
   */
  const magicConstants = {
    x: 0,
    y: 0.08173690932311624,
  };

  const handleMouseMove = (e: React.MouseEvent<HTMLImageElement>) => {
    const img = imgRef.current;

    if (!img) {
      return;
    }

    const rect = getContainedImageSize(img);
    const x = (e.clientX - rect.left) / rect.width - magicConstants.x;
    const y = (e.clientY - rect.top) / rect.height - magicConstants.y;
    const clampedX = Math.max(0, Math.min(1, x));
    const clampedY = Math.max(0, Math.min(1, y));

    setPosition({ x: clampedX, y: clampedY });
  };

  useEffect(() => {
    function handleEscKey(e: KeyboardEvent) {
      if (modalOpen && e.key === "Escape") {
        closeModal();
      }
    }
    document.addEventListener("keydown", handleEscKey);
    return () => {
      document.removeEventListener("keydown", handleEscKey);
    };
  }, [modalOpen]);

  return (
    <div>
      <img {...props} onClick={openModal} className={classes.img} />
      {modalOpen && (
        <div className={classes.modalContainer}>
          <span className={classes.modalCloseButton} onClick={closeModal}>
            &times;
          </span>

          {captureClick ? (
            <img
              {...props}
              ref={imgRef}
              className={classes.modalImg}
              onClick={sendClick}
              onMouseMove={handleMouseMove}
            />
          ) : (
            <img
              {...props}
              className={classes.modalImg}
              onClick={() => setZoom(!zoom)}
            />
          )}
        </div>
      )}
    </div>
  );
}

export { ZoomableImage };
