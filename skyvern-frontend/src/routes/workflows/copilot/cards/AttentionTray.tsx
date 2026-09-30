import type { HTMLAttributes, ReactNode } from "react";
import { ChevronDownIcon, ChevronUpIcon } from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
import { cn } from "@/util/utils";

const TRAY_FRAME =
  "rounded-t-lg border border-b-0 border-amber-500/50 bg-amber-500/[0.06]";
const TRAY_TITLE = "font-semibold text-amber-700 dark:text-yellow-400";

// How the chat asks a request card to render itself as the docked tray.
export interface AttentionTrayPresentation {
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  upNext?: string | null;
}

type AttentionTrayProps = Omit<HTMLAttributes<HTMLDivElement>, "title"> & {
  title: ReactNode;
  titleId?: string;
  // For a request whose title names what it needs, so an ellipsis cannot drop the site or account.
  wrapTitle?: boolean;
  meta?: ReactNode;
  collapsedTitle: ReactNode;
  collapsedMeta?: ReactNode;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  minimizeLabel: string;
  // Another request waiting behind this one; the chat shows one at a time.
  upNext?: string | null;
  children: ReactNode;
};

// The docked frame above the composer for anything Copilot is waiting on the user for.
export function AttentionTray({
  title,
  titleId,
  wrapTitle = false,
  meta,
  collapsedTitle,
  collapsedMeta,
  collapsed,
  onCollapsedChange,
  minimizeLabel,
  upNext,
  children,
  className,
  ...groupProps
}: AttentionTrayProps) {
  if (collapsed) {
    return (
      <div
        className={cn(
          "flex items-center gap-2 px-3 py-1.5 text-xs",
          TRAY_FRAME,
        )}
      >
        <span
          aria-hidden
          className="size-2 shrink-0 rounded-full bg-amber-500"
        />
        <span className={cn("min-w-0 flex-1 truncate", TRAY_TITLE)}>
          {collapsedTitle}
        </span>
        {collapsedMeta}
        <Button
          size="sm"
          variant="ghost"
          className="h-6 px-2 text-xs"
          aria-expanded={false}
          // The collapsed line drops the group, so the button carries its name for screen readers.
          aria-label={
            groupProps["aria-label"]
              ? `Show ${groupProps["aria-label"]}`
              : undefined
          }
          onClick={() => onCollapsedChange(false)}
        >
          Show
          <ChevronUpIcon className="ml-1 size-3.5" />
        </Button>
      </div>
    );
  }

  return (
    <div
      role="group"
      {...groupProps}
      className={cn(
        "flex max-h-[50vh] min-w-0 flex-col overflow-hidden",
        TRAY_FRAME,
        className,
      )}
    >
      {upNext ? (
        <div className="flex min-w-0 items-center gap-2 border-b border-amber-500/25 px-3 py-1 text-xs text-muted-foreground">
          <span
            aria-hidden
            className="size-1.5 shrink-0 rounded-full bg-amber-500"
          />
          <span className="shrink-0">Up next ·</span>
          <span className="min-w-0 truncate font-medium text-amber-700 dark:text-yellow-400">
            {upNext}
          </span>
        </div>
      ) : null}
      <div className="flex items-center gap-2 px-3 pb-1 pt-2 text-xs">
        <span
          aria-hidden
          className="size-2 shrink-0 rounded-full bg-amber-500 shadow-[0_0_0_3px_rgba(245,158,11,0.18)]"
        />
        <span
          id={titleId}
          className={cn(
            "min-w-0",
            wrapTitle ? "break-words" : "truncate",
            TRAY_TITLE,
          )}
        >
          {title}
        </span>
        <div className="ml-auto flex shrink-0 items-center gap-1">
          {meta}
          <Button
            size="icon"
            variant="ghost"
            className="size-6 text-muted-foreground"
            aria-label={minimizeLabel}
            aria-expanded
            onClick={() => onCollapsedChange(true)}
          >
            <ChevronDownIcon className="size-3.5" />
          </Button>
        </div>
      </div>
      {children}
    </div>
  );
}

// Where a docked request was raised in the transcript. While it is pending the answer happens in
// the tray, so this row only points there.
export function AttentionMarker({
  icon,
  title,
  hint,
}: {
  icon: ReactNode;
  title: string;
  hint: string;
}) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-dashed border-amber-500/50 px-3 py-2 text-xs text-muted-foreground">
      {icon}
      <span className="min-w-0">
        <span className={TRAY_TITLE}>{title}</span> · {hint}
      </span>
    </div>
  );
}
