import { useEffect, useRef, useState, type RefObject } from "react";
import { ExitIcon, GlobeIcon, HandIcon } from "@radix-ui/react-icons";
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
  centered?: boolean;
  // Only wired up by callers that want the URL bar to double as a navigation
  // input (currently: the hosted-browser-session live view). Omitted, the bar
  // stays the plain read-only display every other caller already gets.
  onNavigate?: (url: string) => void;
  navigateError?: string | null;
}

function UrlBar({ url }: { url: string }) {
  return (
    <div className="flex h-8 w-full items-center gap-2 rounded-t-md bg-slate-800 px-3 text-xs text-slate-300">
      <GlobeIcon className="h-3 w-3 flex-shrink-0 text-slate-400" />
      <span className="truncate">{url}</span>
    </div>
  );
}

function NavigableUrlBar({
  url,
  onNavigate,
  navigateError,
}: {
  url: string;
  onNavigate: (url: string) => void;
  navigateError?: string | null;
}) {
  const [value, setValue] = useState(url);
  const isFocusedRef = useRef(false);

  useEffect(() => {
    // Don't stomp a URL the user is mid-typing if the remote page navigates elsewhere
    // (e.g. an in-flight action) while they still have the input focused.
    if (!isFocusedRef.current) {
      setValue(url);
    }
  }, [url]);

  return (
    <form
      className="flex h-8 w-full items-center gap-2 rounded-t-md bg-slate-800 px-2"
      onSubmit={(e) => {
        e.preventDefault();
        const trimmed = value.trim();
        if (trimmed) {
          onNavigate(trimmed);
        }
      }}
    >
      <GlobeIcon className="h-3 w-3 flex-shrink-0 text-slate-400" />
      <Input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onFocus={() => {
          isFocusedRef.current = true;
        }}
        onBlur={() => {
          isFocusedRef.current = false;
        }}
        onClick={(e) => e.stopPropagation()}
        // The stream container forwards every keystroke to the remote CDP session
        // (see InteractiveStreamView's onKeyDown/onKeyUp); typing a URL must not
        // also leak as keyboard input to the page being viewed.
        onKeyDown={(e) => e.stopPropagation()}
        onKeyUp={(e) => e.stopPropagation()}
        placeholder="Enter a URL and press Enter"
        className="h-6 flex-1 border-none bg-transparent px-1 text-xs text-slate-100 shadow-none focus-visible:ring-1 focus-visible:ring-slate-500"
      />
      {navigateError && (
        <span className="flex-shrink-0 truncate text-xs text-red-400">
          {navigateError}
        </span>
      )}
    </form>
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
  centered,
  onNavigate,
  navigateError,
}: InteractiveStreamViewProps) {
  const imgDataUrl = `data:image/${streamFormat};base64,${streamImgSrc}`;

  if (interactive) {
    const showNavigableBar =
      Boolean(onNavigate) && userIsControlling && currentUrl !== undefined;
    const showReadOnlyBar = !showNavigableBar && Boolean(currentUrl);
    const showUrlBar = showNavigableBar || showReadOnlyBar;

    return (
      <div
        ref={containerRef}
        className="relative h-full w-full outline-none"
        tabIndex={0}
        onKeyDown={handlers.handleKeyDown}
        onKeyUp={handlers.handleKeyUp}
      >
        {showNavigableBar && (
          <NavigableUrlBar
            url={currentUrl ?? ""}
            onNavigate={onNavigate!}
            navigateError={navigateError}
          />
        )}
        {showReadOnlyBar && <UrlBar url={currentUrl!} />}
        {showControlButtons && !userIsControlling && inputReady && (
          <div className="absolute inset-0 z-10 flex items-center justify-center">
            <Button
              size="sm"
              className="border"
              onClick={() => setUserIsControlling(true)}
            >
              <HandIcon className="mr-2 h-4 w-4" />
              take control
            </Button>
          </div>
        )}
        {showControlButtons && userIsControlling && (
          <Button
            size="sm"
            className="absolute bottom-2 left-1/2 z-10 -translate-x-1/2 border"
            onClick={() => setUserIsControlling(false)}
          >
            <ExitIcon className="mr-2 h-4 w-4" />
            stop controlling
          </Button>
        )}
        <img
          src={imgDataUrl}
          className={cn(
            "w-full rounded-md object-contain",
            showUrlBar ? "h-[calc(100%-2rem)]" : "h-full",
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

  // Plain img (not ZoomableImage) so h-full resolves here; ZoomableImage's bare
  // auto-height wrapper collapses the height and pins the frame to the top.
  if (centered) {
    return (
      <div className="flex h-full w-full flex-col">
        {currentUrl && <UrlBar url={currentUrl} />}
        <img
          src={imgDataUrl}
          className={cn(
            "min-h-0 w-full flex-1 object-contain",
            currentUrl ? "rounded-b-md" : "rounded-md",
          )}
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
