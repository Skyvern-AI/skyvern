import type { RefObject } from "react";
import { GlobeIcon } from "@radix-ui/react-icons";
import { ZoomableImage } from "@/components/ZoomableImage";
import { Button } from "@/components/ui/button";
import { cn } from "@/util/utils";

interface InteractiveStreamViewProps {
  streamImgSrc: string;
  streamFormat: string;
  interactive: boolean;
  userIsControlling: boolean;
  setUserIsControlling: (v: boolean) => void;
  inputReady: boolean;
  containerRef: RefObject<HTMLDivElement>;
  showControlButtons: boolean;
  handlers: {
    handleMouseDown: (e: React.MouseEvent<HTMLImageElement>) => void;
    handleMouseUp: (e: React.MouseEvent<HTMLImageElement>) => void;
    handleMouseMove: (e: React.MouseEvent<HTMLImageElement>) => void;
    handleKeyDown: (e: React.KeyboardEvent) => void;
    handleKeyUp: (e: React.KeyboardEvent) => void;
  };
  currentUrl?: string;
}

function UrlBar({ url }: { url: string }) {
  return (
    <div className="flex h-8 w-full items-center gap-2 rounded-t-md bg-slate-800 px-3 text-xs text-slate-300">
      <GlobeIcon className="h-3 w-3 flex-shrink-0 text-slate-400" />
      <span className="truncate">{url}</span>
    </div>
  );
}

function InteractiveStreamView({
  streamImgSrc,
  streamFormat,
  interactive,
  userIsControlling,
  setUserIsControlling,
  inputReady,
  containerRef,
  showControlButtons,
  handlers,
  currentUrl,
}: InteractiveStreamViewProps) {
  const imgDataUrl = `data:image/${streamFormat};base64,${streamImgSrc}`;

  if (interactive) {
    return (
      <div
        ref={containerRef}
        className="relative h-full w-full outline-none"
        tabIndex={0}
        onKeyDown={handlers.handleKeyDown}
        onKeyUp={handlers.handleKeyUp}
      >
        {currentUrl && <UrlBar url={currentUrl} />}
        {showControlButtons && !userIsControlling && inputReady && (
          <div className="absolute inset-0 z-10 flex items-center justify-center">
            <Button onClick={() => setUserIsControlling(true)}>
              take control
            </Button>
          </div>
        )}
        {showControlButtons && userIsControlling && (
          <Button
            className="absolute bottom-2 left-1/2 z-10 -translate-x-1/2"
            onClick={() => setUserIsControlling(false)}
          >
            stop controlling
          </Button>
        )}
        <img
          src={imgDataUrl}
          className={cn(
            "w-full rounded-md object-contain",
            currentUrl ? "h-[calc(100%-2rem)]" : "h-full",
            { "cursor-default": userIsControlling },
          )}
          onMouseDown={handlers.handleMouseDown}
          onMouseUp={handlers.handleMouseUp}
          onMouseMove={handlers.handleMouseMove}
          onContextMenu={(e) => e.preventDefault()}
          draggable={false}
        />
      </div>
    );
  }

  return (
    <div className="h-full w-full">
      {currentUrl && <UrlBar url={currentUrl} />}
      <ZoomableImage
        src={imgDataUrl}
        className={
          currentUrl ? "h-[calc(100%-2rem)] rounded-b-md" : "rounded-md"
        }
      />
    </div>
  );
}

export { InteractiveStreamView };
