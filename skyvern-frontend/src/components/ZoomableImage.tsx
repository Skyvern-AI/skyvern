import { useEffect, useState } from "react";
import clsx from "clsx";

type HTMLImageElementProps = React.ComponentProps<"img">;

function ZoomableImage(props: HTMLImageElementProps) {
  const [modalOpen, setModalOpen] = useState(false);
  const [zoom, setZoom] = useState(false);

  const openModal = () => {
    setZoom(false);
    setModalOpen(true);
  };

  const closeModal = () => {
    setModalOpen(false);
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
      <img
        {...props}
        onClick={openModal}
        className={clsx("cursor-pointer object-contain", props.className)}
      />
      {modalOpen && (
        <div
          className={clsx(
            "fixed inset-0 z-50 flex justify-center bg-black bg-opacity-75 overflow-auto p-16",
            {
              "items-center": !zoom,
              "items-baseline": zoom,
            },
          )}
        >
          <span
            className="absolute top-4 right-4 text-white text-4xl cursor-pointer"
            onClick={closeModal}
          >
            &times;
          </span>
          <img
            {...props}
            onClick={() => setZoom(!zoom)}
            className={clsx({
              "min-h-full object-contain h-full w-full m-0 cursor-zoom-in max-h-full max-w-full":
                !zoom,
              "min-h-full object-contain m-0 cursor-zoom-out max-w-none max-h-none mr-auto ml-auto":
                zoom,
            })}
          />
        </div>
      )}
    </div>
  );
}

export { ZoomableImage };
