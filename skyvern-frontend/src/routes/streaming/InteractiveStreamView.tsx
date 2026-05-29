import { useEffect, useState, type FormEvent, type RefObject } from "react";
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  GlobeIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { ZoomableImage } from "@/components/ZoomableImage";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/util/utils";

interface InteractiveStreamViewProps {
  streamImgSrc: string;
  streamFormat: string;
  interactive: boolean;
  userIsControlling: boolean;
  setUserIsControlling: (v: boolean) => void;
  inputReady: boolean;
  browserCommandError?: string | null;
  containerRef: RefObject<HTMLDivElement>;
  showControlButtons: boolean;
  handlers: {
    handleMouseDown: (e: React.MouseEvent<HTMLImageElement>) => void;
    handleMouseUp: (e: React.MouseEvent<HTMLImageElement>) => void;
    handleMouseMove: (e: React.MouseEvent<HTMLImageElement>) => void;
    handleKeyDown: (e: React.KeyboardEvent) => void;
    handleKeyUp: (e: React.KeyboardEvent) => void;
  };
  browserControls?: {
    navigate: (url: string) => void;
    reload: () => void;
    goBack: () => void;
    goForward: () => void;
  };
  currentUrl?: string;
}

function UrlBar({
  url,
  inputReady,
  browserCommandError,
  browserControls,
}: {
  url: string;
  inputReady: boolean;
  browserCommandError?: string | null;
  browserControls?: InteractiveStreamViewProps["browserControls"];
}) {
  const [draftUrl, setDraftUrl] = useState(url);
  const controlsEnabled = inputReady && Boolean(browserControls);

  useEffect(() => {
    setDraftUrl(url);
  }, [url]);

  function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    e.stopPropagation();
    browserControls?.navigate(draftUrl);
  }

  return (
    <div
      className="flex h-8 w-full items-center gap-1 rounded-t-md bg-slate-800 px-2 text-xs text-slate-300"
      onKeyDown={(e) => e.stopPropagation()}
      onKeyUp={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      onMouseUp={(e) => e.stopPropagation()}
    >
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="h-6 w-6 flex-shrink-0 text-slate-300 hover:bg-slate-700 hover:text-white"
        disabled={!controlsEnabled}
        aria-label="Go back"
        title="Go back"
        onClick={() => browserControls?.goBack()}
      >
        <ArrowLeftIcon className="h-3.5 w-3.5" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="h-6 w-6 flex-shrink-0 text-slate-300 hover:bg-slate-700 hover:text-white"
        disabled={!controlsEnabled}
        aria-label="Go forward"
        title="Go forward"
        onClick={() => browserControls?.goForward()}
      >
        <ArrowRightIcon className="h-3.5 w-3.5" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="h-6 w-6 flex-shrink-0 text-slate-300 hover:bg-slate-700 hover:text-white"
        disabled={!controlsEnabled}
        aria-label="Reload"
        title="Reload"
        onClick={() => browserControls?.reload()}
      >
        <ReloadIcon className="h-3.5 w-3.5" />
      </Button>
      <form className="min-w-0 flex-1" onSubmit={handleSubmit}>
        <div className="relative">
          <GlobeIcon className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-slate-400" />
          <Input
            value={draftUrl}
            disabled={!controlsEnabled}
            aria-label="Current browser URL"
            title={browserCommandError || draftUrl}
            onChange={(e) => setDraftUrl(e.target.value)}
            className={cn(
              "h-6 rounded border-slate-700 bg-slate-900 py-0 pl-7 pr-2 text-xs text-slate-200 shadow-none focus-visible:ring-slate-500",
              browserCommandError ? "border-red-500 text-red-200" : "",
            )}
          />
        </div>
      </form>
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
  browserCommandError,
  containerRef,
  showControlButtons,
  handlers,
  browserControls,
  currentUrl,
}: InteractiveStreamViewProps) {
  const imgDataUrl = `data:image/${streamFormat};base64,${streamImgSrc}`;
  const showUrlBar = Boolean(currentUrl) || Boolean(browserControls);

  if (interactive) {
    return (
      <div
        ref={containerRef}
        className="relative h-full w-full outline-none"
        tabIndex={0}
        onKeyDown={handlers.handleKeyDown}
        onKeyUp={handlers.handleKeyUp}
      >
        {showUrlBar && (
          <UrlBar
            url={currentUrl ?? ""}
            inputReady={inputReady}
            browserCommandError={browserCommandError}
            browserControls={browserControls}
          />
        )}
        {showControlButtons && !userIsControlling && inputReady && (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-[radial-gradient(circle_at_center,rgba(15,23,42,0.22)_0%,rgba(15,23,42,0.12)_18%,transparent_42%)]">
            <Button
              aria-label="Take control of this browser session"
              className="group pointer-events-auto h-10 rounded-full border border-white/15 bg-slate-950/90 px-5 text-sm font-semibold text-white shadow-xl shadow-slate-950/25 ring-1 ring-slate-950/10 backdrop-blur-sm transition duration-200 ease-out hover:-translate-y-0.5 hover:bg-slate-900 hover:shadow-2xl focus-visible:ring-2 focus-visible:ring-sky-400 focus-visible:ring-offset-2 motion-reduce:transition-none motion-reduce:hover:translate-y-0"
              onClick={() => setUserIsControlling(true)}
            >
              Take the wheel
              <span
                aria-hidden="true"
                className="ml-2 transition-transform duration-200 group-hover:translate-x-0.5 motion-reduce:transition-none"
              >
                →
              </span>
            </Button>
          </div>
        )}
        {showControlButtons && userIsControlling && (
          <Button
            aria-label="Stop controlling this browser session"
            className="absolute bottom-2 left-1/2 z-10 h-9 -translate-x-1/2 rounded-full border border-white/15 bg-slate-950/90 px-4 text-sm font-semibold text-white shadow-xl shadow-slate-950/25 ring-1 ring-slate-950/10 backdrop-blur-sm transition duration-200 ease-out hover:-translate-x-1/2 hover:-translate-y-0.5 hover:bg-slate-900 focus-visible:ring-2 focus-visible:ring-sky-400 focus-visible:ring-offset-2 motion-reduce:transition-none motion-reduce:hover:-translate-x-1/2 motion-reduce:hover:translate-y-0"
            onClick={() => setUserIsControlling(false)}
          >
            Release control
          </Button>
        )}
        <img
          src={imgDataUrl}
          className={cn(
            "w-full rounded-md object-contain",
            showUrlBar ? "h-[calc(100%-2rem)] rounded-t-none" : "h-full",
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
      {showUrlBar && (
        <UrlBar
          url={currentUrl ?? ""}
          inputReady={inputReady}
          browserCommandError={browserCommandError}
          browserControls={browserControls}
        />
      )}
      <ZoomableImage
        src={imgDataUrl}
        className={
          showUrlBar ? "h-[calc(100%-2rem)] rounded-b-md" : "rounded-md"
        }
      />
    </div>
  );
}

export { InteractiveStreamView };
