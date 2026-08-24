import { useEffect, useRef, useState, type RefObject } from "react";
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  ExitIcon,
  GlobeIcon,
  HandIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { ZoomableImage } from "@/components/ZoomableImage";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";
import type { HistoryAction } from "./useCdpInput";

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
    handlePaste: (e: React.ClipboardEvent) => void;
  };
  currentUrl?: string;
  centered?: boolean;
  // Only wired up by callers that want the URL bar to double as a navigation
  // input (currently: the hosted-browser-session live view). Omitted, the bar
  // stays the plain read-only display every other caller already gets.
  onNavigate?: (url: string) => void;
  navigateError?: string | null;
  // Wired alongside onNavigate by the live view: drives the chrome's back/forward/
  // reload controls. Omitted, the chrome renders as a URL bar with no nav buttons.
  onHistoryNavigate?: (action: HistoryAction) => void;
  // Passing this hands the frame to the caller: it receives the measured preview
  // width and is expected to draw the window (border, rounding, width) around us,
  // so we render flush instead. Omitted, we keep framing ourselves.
  onFrameWidthChange?: (width: number | null) => void;
  frameToken?: number;
  onFrameLoad?: (token: number) => void;
}

function UrlBar({
  url,
  disabled,
  className,
}: {
  url: string;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn(
        // px-2 matches NavigableUrlBar's form padding so the icon doesn't jump
        // sideways when the bar swaps between the two on take/release control.
        "flex h-8 w-full items-center gap-2 rounded-t-md bg-slate-800 px-2 text-xs text-slate-300",
        disabled && "cursor-not-allowed opacity-50",
        className,
      )}
      title={disabled ? "Take control to edit the URL" : undefined}
    >
      <GlobeIcon className="h-3 w-3 flex-shrink-0 text-slate-400" />
      <span className="truncate">{url}</span>
    </div>
  );
}

function NavButton({
  label,
  icon,
  disabled,
  onClick,
}: {
  label: string;
  icon: React.ReactNode;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <Tooltip>
      {/* A disabled button emits no pointer events, so the trigger has to be the
          wrapper for the take-control hint to be reachable at all. */}
      <TooltipTrigger asChild>
        <span className="flex">
          <button
            type="button"
            aria-label={label}
            disabled={disabled}
            onClick={onClick}
            className={cn(
              "flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full text-slate-300",
              "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-slate-500",
              disabled
                ? "cursor-not-allowed opacity-50"
                : "hover:bg-slate-700 hover:text-foreground",
            )}
          >
            {icon}
          </button>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        {disabled ? "Take control to navigate" : label}
      </TooltipContent>
    </Tooltip>
  );
}

function NavigableUrlBar({
  url,
  onNavigate,
  navigateError,
  className,
}: {
  url: string;
  onNavigate: (url: string) => void;
  navigateError?: string | null;
  className?: string;
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
      className={cn(
        "flex h-8 w-full items-center gap-2 rounded-t-md bg-slate-800 px-2",
        className,
      )}
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
        onPaste={(e) => e.stopPropagation()}
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
  onHistoryNavigate,
  onFrameWidthChange,
  frameToken,
  onFrameLoad,
}: InteractiveStreamViewProps) {
  const imgDataUrl = `data:image/${streamFormat};base64,${streamImgSrc}`;
  const imgRef = useRef<HTMLImageElement>(null);
  // object-contain lets the img box itself go wider than the actually-visible (letterboxed)
  // picture, so the bar can't just be "the img's width" via CSS alone -- it has to track the
  // rendered content box directly. ResizeObserver reports that box on every frame/size change.
  //
  // We derive width from the observed *height* and the image's intrinsic aspect ratio rather
  // than reading contentRect.width directly: previewWidth is fed back as this same element's
  // `width` style, so measuring width here would create a feedback loop that can only shrink,
  // never grow back on a wider layout. Height isn't constrained by that style, so it stays a
  // trustworthy input.
  const [previewWidth, setPreviewWidth] = useState<number | null>(null);
  // Nothing pushes a load-complete event to the client, and the screencast repaints
  // continuously, so a reload is otherwise invisible: spin for a fixed beat purely to
  // acknowledge the click.
  const [isReloading, setIsReloading] = useState(false);
  const parentOwnsFrame = Boolean(onFrameWidthChange);
  const onFrameWidthChangeRef = useRef(onFrameWidthChange);

  useEffect(() => {
    onFrameWidthChangeRef.current = onFrameWidthChange;
  }, [onFrameWidthChange]);

  useEffect(() => {
    const el = imgRef.current;
    if (!el) {
      return;
    }
    const observer = new ResizeObserver((entries) => {
      const height = entries[0]?.contentRect.height;
      const { naturalWidth, naturalHeight } = el;
      if (height && naturalWidth && naturalHeight) {
        const width = Math.round((height * naturalWidth) / naturalHeight);
        setPreviewWidth(width);
        onFrameWidthChangeRef.current?.(width);
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Leave the parent's frame full-width again rather than stuck at the width of a
  // preview that is no longer on screen.
  useEffect(() => () => onFrameWidthChangeRef.current?.(null), []);

  useEffect(() => {
    if (!isReloading) {
      return;
    }
    const timeout = setTimeout(() => setIsReloading(false), 900);
    return () => clearTimeout(timeout);
  }, [isReloading]);

  if (interactive) {
    const showNavigableBar =
      Boolean(onNavigate) && userIsControlling && currentUrl !== undefined;
    const showReadOnlyBar = !showNavigableBar && Boolean(currentUrl);
    const showUrlBar = showNavigableBar || showReadOnlyBar;

    // Inside the chrome the bar is a pill sitting in the toolbar row, so it drops the
    // full-width/top-rounding the standalone bar carries. px-3 stays matched across both
    // so the globe doesn't shift sideways on take/release control.
    const pillClassName = "h-7 flex-1 rounded-full bg-slate-900 px-3";

    const urlBar = showNavigableBar ? (
      <NavigableUrlBar
        url={currentUrl ?? ""}
        onNavigate={onNavigate!}
        navigateError={navigateError}
        className={pillClassName}
      />
    ) : showReadOnlyBar ? (
      <UrlBar
        url={currentUrl!}
        disabled={Boolean(onNavigate) && !userIsControlling}
        className={pillClassName}
      />
    ) : null;

    // Back/forward/reload always no-op safely at the ends of history (the server checks
    // the stack), so they stay enabled rather than needing a pushed can-go-back state.
    const navDisabled = !userIsControlling;
    const browserChrome = showUrlBar ? (
      <div className="flex w-full flex-shrink-0 items-center gap-1 bg-slate-800 px-2 py-1.5">
        {onHistoryNavigate && (
          <TooltipProvider delayDuration={200}>
            <NavButton
              label="Back"
              icon={<ArrowLeftIcon className="h-4 w-4" />}
              disabled={navDisabled}
              onClick={() => onHistoryNavigate("back")}
            />
            <NavButton
              label="Forward"
              icon={<ArrowRightIcon className="h-4 w-4" />}
              disabled={navDisabled}
              onClick={() => onHistoryNavigate("forward")}
            />
            <NavButton
              label="Reload"
              icon={
                <ReloadIcon
                  className={cn("h-3.5 w-3.5", isReloading && "animate-spin")}
                />
              }
              disabled={navDisabled}
              onClick={() => {
                setIsReloading(true);
                onHistoryNavigate("reload");
              }}
            />
          </TooltipProvider>
        )}
        {urlBar}
      </div>
    ) : null;

    const imgInteractionProps = {
      src: imgDataUrl,
      "data-frame-token": frameToken,
      onLoad: () => onFrameLoad?.(frameToken ?? 0),
      onMouseDown: handlers.handleMouseDown,
      onMouseUp: handlers.handleMouseUp,
      onMouseMove: handlers.handleMouseMove,
      onContextMenu: (e: React.MouseEvent) => e.preventDefault(),
      draggable: false,
    };

    const controlOverlays = (
      <>
        {showControlButtons && !userIsControlling && inputReady && (
          // The overlay covers the whole picture, so a click anywhere on it is
          // someone trying to use the page: take control instead of eating the
          // click (this layer alone drew ~470 dead clicks from a fifth of studio
          // users). The button stays the keyboard/screen-reader path.
          <div
            data-testid="take-control-overlay"
            className="absolute inset-0 z-10 flex cursor-pointer items-center justify-center"
            onClick={() => setUserIsControlling(true)}
          >
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
      </>
    );

    return (
      <div
        ref={containerRef}
        className="relative h-full w-full outline-none"
        tabIndex={0}
        onKeyDown={handlers.handleKeyDown}
        onKeyUp={handlers.handleKeyUp}
        onPaste={handlers.handlePaste}
      >
        {/* Chrome and viewport share previewWidth so the window sizes to the letterboxed
            picture rather than the pane, the way a real browser window frames its page.
            When the parent owns the frame it applies that width (and the border) itself,
            around its own tab strip too, so here we just fill it.
            previewWidth comes from the height alone, so a pane taller than the picture's
            aspect ratio (the studio's browser pane) asks for more width than it has;
            max-w-full keeps the window inside it and letterboxes instead of cropping. */}
        <div
          className={cn(
            "mx-auto flex h-full max-w-full flex-col items-center overflow-hidden",
            !parentOwnsFrame && "rounded-md shadow-elevated",
          )}
          style={
            !parentOwnsFrame && previewWidth
              ? { width: previewWidth }
              : undefined
          }
        >
          {browserChrome}
          <img
            ref={imgRef}
            {...imgInteractionProps}
            className={cn(
              "min-h-0 w-auto max-w-full flex-1 object-contain",
              !parentOwnsFrame && (showUrlBar ? "rounded-b-md" : "rounded-md"),
              { "cursor-default": userIsControlling },
            )}
          />
        </div>
        {controlOverlays}
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
          data-frame-token={frameToken}
          onLoad={() => onFrameLoad?.(frameToken ?? 0)}
          decoding="async"
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
