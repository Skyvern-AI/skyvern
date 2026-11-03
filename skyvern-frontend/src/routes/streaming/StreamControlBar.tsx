import { ClipboardIcon, ExitIcon, HandIcon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import { isMacPlatform } from "@/util/platform";
import { cn } from "@/util/utils";

const PASTE_SHORTCUT = isMacPlatform() ? "⌘V" : "Ctrl+V";

export function PastedNotice({ characters }: { characters: number | null }) {
  return (
    // Stays mounted so screen readers announce the text when it changes.
    <div
      role="status"
      className="pointer-events-none absolute bottom-14 left-1/2 z-10 -translate-x-1/2"
    >
      {characters != null && (
        <div className="whitespace-nowrap rounded-md bg-foreground px-3 py-1.5 text-xs text-background shadow-md">
          Sent {characters} {characters === 1 ? "character" : "characters"} to
          the browser
        </div>
      )}
    </div>
  );
}

export function StreamControlBar({
  onStop,
  onPaste,
}: {
  onStop: () => void;
  // Omitted when the stream can't paste; the bar then hides its paste controls.
  onPaste?: () => void;
}) {
  return (
    <div
      className="pointer-events-auto absolute bottom-2 left-1/2 z-10 flex max-w-[calc(100%-1rem)] -translate-x-1/2 items-center gap-2 overflow-hidden whitespace-nowrap rounded-md border bg-background py-1 pl-3 pr-1 text-xs text-foreground shadow-md"
      // The stream container forwards keys to the remote page; Enter/Space here
      // must activate the bar's own buttons instead.
      onKeyDown={(e) => e.stopPropagation()}
      onKeyUp={(e) => e.stopPropagation()}
    >
      <span className="flex min-w-0 items-center gap-1.5 font-medium">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-destructive" />
        <span className="truncate">You're in control</span>
      </span>
      {onPaste && (
        <>
          {/* The bar measures ~511px with the full hint, ~382px with the key alone. */}
          <span className="hidden shrink-0 items-center gap-1.5 text-muted-foreground [@container_stream_(min-width:420px)]:flex">
            <kbd className="rounded border bg-muted px-1.5 font-mono text-[11px] text-foreground">
              {PASTE_SHORTCUT}
            </kbd>
            <span className="hidden [@container_stream_(min-width:540px)]:inline">
              pastes your clipboard
            </span>
          </span>
          <Button
            size="sm"
            variant="outline"
            // Too narrow for both buttons: keep Stop controlling, the only way out.
            className="h-7 shrink-0 px-2.5 text-xs [@container_stream_(max-width:280px)]:hidden"
            onClick={onPaste}
          >
            <ClipboardIcon className="mr-1.5 h-3.5 w-3.5" />
            Paste
          </Button>
        </>
      )}
      <Button
        size="sm"
        className="h-7 shrink-0 px-2.5 text-xs"
        onClick={onStop}
      >
        <ExitIcon className="mr-1.5 h-3.5 w-3.5" />
        Stop controlling
      </Button>
    </div>
  );
}

export function TakeControlButton({
  onClick,
  className,
}: {
  onClick: () => void;
  className?: string;
}) {
  // Same surface as StreamControlBar so the two control states read as one set.
  return (
    <Button
      size="sm"
      variant="outline"
      className={cn(
        "border-border bg-background text-foreground shadow-md",
        className,
      )}
      onClick={onClick}
    >
      <HandIcon className="mr-2 h-4 w-4" />
      Take control
    </Button>
  );
}
