import { ChevronDownIcon } from "@radix-ui/react-icons";
import type { ReactNode } from "react";

import { cn } from "@/util/utils";

import { ICON_COLUMN } from "./cardLayout";

export type PillTone = "green" | "amber" | "sky" | "red";

const PILL_TONE_CLASSES: Record<PillTone, string> = {
  green:
    "border-emerald-500/30 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  amber:
    "border-amber-500/30 bg-amber-500/15 text-amber-700 dark:text-amber-300",
  sky: "border-sky-500/30 bg-sky-500/15 text-sky-700 dark:text-sky-300",
  red: "border-red-500/30 bg-red-500/15 text-red-700 dark:text-red-300",
};

export function CardPill({
  tone,
  children,
}: {
  tone: PillTone;
  children: ReactNode;
}) {
  return (
    <span
      className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${PILL_TONE_CLASSES[tone]}`}
    >
      {children}
    </span>
  );
}

export function CopilotCard({
  id,
  className,
  children,
}: {
  id?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      id={id}
      className={cn(
        "min-w-0 rounded-lg border border-border bg-slate-elevation2",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function CardHeader({
  icon,
  title,
  meta,
  right,
  actions,
  wrapTitle = false,
  expanded,
  onToggle,
}: {
  icon: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  right?: ReactNode;
  // Interactive controls for the header row. They render outside the toggle, since a button
  // cannot nest inside the one that expands the card.
  actions?: ReactNode;
  // For a card still waiting on the user, whose title must not lose words to an ellipsis.
  wrapTitle?: boolean;
  expanded?: boolean;
  onToggle?: () => void;
}) {
  const label = (
    <>
      <span className={ICON_COLUMN}>{icon}</span>
      <span
        title={
          !wrapTitle && typeof title === "string"
            ? [title, typeof meta === "string" ? meta : null]
                .filter(Boolean)
                .join(" · ")
            : undefined
        }
        className={`min-w-0 flex-1 text-left text-xs ${wrapTitle ? "break-words" : "truncate"}`}
      >
        <span className="font-semibold text-foreground">{title}</span>
        {meta ? <span className="text-muted-foreground"> · {meta}</span> : null}
      </span>
      {right ? (
        <span className="flex shrink-0 items-center gap-2">{right}</span>
      ) : null}
    </>
  );
  const chevron = (
    <ChevronDownIcon
      aria-hidden="true"
      className={`h-4 w-4 shrink-0 text-muted-foreground transition-transform ${expanded ? "rotate-180" : ""}`}
    />
  );
  const rowClass = `flex w-full min-w-0 items-center gap-2.5 px-3 ${wrapTitle ? "min-h-10 py-2" : "h-10"}`;
  const focusRing =
    "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring";
  if (!onToggle) {
    return (
      <div className={rowClass}>
        {label}
        {actions}
      </div>
    );
  }
  if (actions) {
    return (
      <div className={`${rowClass} rounded-lg hover:bg-slate-elevation3`}>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          className={`flex h-full min-w-0 flex-1 items-center gap-2.5 rounded-md ${focusRing}`}
        >
          {label}
        </button>
        {actions}
        {/* A mouse-only hit target for the same toggle; the labelled button is the one a keyboard
            or screen reader reaches, and a span takes no focus that assistive tech cannot see. */}
        <span
          aria-hidden="true"
          onClick={onToggle}
          className="flex h-full shrink-0 cursor-pointer items-center"
        >
          {chevron}
        </span>
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={expanded}
      className={`${rowClass} rounded-lg hover:bg-slate-elevation3 ${focusRing}`}
    >
      {label}
      {chevron}
    </button>
  );
}

export function CardBody({ children }: { children: ReactNode }) {
  return <div className="px-3 pb-3">{children}</div>;
}

// The first control's left edge sits on the icon column and the last control's right edge on the
// header's pill/chevron edge, because the band shares the header's px-3.
export function CardFooter({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-b-lg border-t border-border/55 bg-slate-elevation1/55 px-3 py-2",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function GutterRow({
  marker,
  markerClass,
  srLabel,
  children,
}: {
  marker?: ReactNode;
  markerClass?: string;
  // What the marker means, for screen readers, which would otherwise read "+" or "tilde".
  srLabel?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex items-baseline gap-2.5 py-px text-xs leading-[1.6]">
      <span
        aria-hidden="true"
        className={`${ICON_COLUMN} font-mono text-[11px] ${markerClass ?? "text-muted-foreground"}`}
      >
        {marker}
      </span>
      <span className="min-w-0 flex-1 break-words text-foreground dark:text-slate-200">
        {srLabel ? <span className="sr-only">{srLabel}: </span> : null}
        {children}
      </span>
    </div>
  );
}
