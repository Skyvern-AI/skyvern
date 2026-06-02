import type { ReactNode } from "react";
import { InfoCircledIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import { AnimatedWave } from "@/components/AnimatedWave";
import { RotateThrough } from "@/components/RotateThrough";

type StreamDiagnostic = {
  title: string;
  detail?: string;
  hint?: string;
  // When true, the panel shows the whimsical "still working on it" animation.
  pending?: boolean;
};

const WHIMSICAL_LOADING_MESSAGES = [
  "Hm, still working on it...",
  "Hang tight, we're almost there...",
  "Reticulating splines...",
  "Backpropagating...",
  "Attention is all I need...",
  "Warming up the pixels...",
  "Consulting the manual...",
  "Teaching the browser some manners...",
  "Looking for the bat phone...",
  "Negotiating with the tubes...",
  "Where's Shu?...",
];

const WHIMSICAL_SPARKLE = "‧₊˚ ⋅ ✦ ✨ ✦ ⋅ ˚₊‧";

const SCREENSHOT_PANEL_CLASS = "bg-slate-elevation1 text-slate-300";

type StreamMode = "cdp" | "vnc" | "fallback" | "unavailable";

const STREAM_MODE_COPY: Record<
  StreamMode,
  { label: string; title: string; className: string }
> = {
  cdp: {
    label: "Local stream",
    title: "Local browser streaming through the backend",
    className:
      "border-cyan-600/30 bg-cyan-600/10 text-cyan-700 dark:border-cyan-500/40 dark:bg-cyan-500/10 dark:text-cyan-200",
  },
  vnc: {
    label: "VNC",
    title: "VNC browser streaming",
    className:
      "border-emerald-600/30 bg-emerald-600/10 text-emerald-700 dark:border-emerald-500/40 dark:bg-emerald-500/10 dark:text-emerald-200",
  },
  fallback: {
    label: "VNC -> Local",
    title: "VNC disconnected; using local browser streaming fallback",
    className:
      "border-amber-600/30 bg-amber-600/10 text-amber-700 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200",
  },
  unavailable: {
    label: "Unavailable",
    title: "Browser streaming is unavailable for this session",
    className:
      "border-neutral-400/40 bg-neutral-500/10 text-neutral-600 dark:border-slate-500/40 dark:bg-slate-500/10 dark:text-slate-300",
  },
};

function StreamModeBadge({
  mode,
  className,
}: {
  mode: StreamMode;
  className?: string;
}) {
  const copy = STREAM_MODE_COPY[mode];
  return (
    <span
      title={copy.title}
      className={cn(
        "inline-flex h-5 items-center rounded border px-2 text-[0.68rem] font-medium uppercase leading-none tracking-normal",
        copy.className,
        className,
      )}
    >
      {copy.label}
    </span>
  );
}

function StreamStatusPanel({
  diagnostic,
  children,
  className,
}: {
  diagnostic: StreamDiagnostic;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex h-full w-full items-center justify-center rounded-md bg-white p-6 text-neutral-600 dark:bg-slate-900 dark:text-slate-300",
        className,
      )}
    >
      <div className="flex max-w-md flex-col gap-2 text-sm">
        <div className="flex items-center gap-2 font-medium text-neutral-900 dark:text-slate-100">
          <InfoCircledIcon className="h-4 w-4 flex-shrink-0 text-neutral-500 dark:text-slate-400" />
          <span>{diagnostic.title}</span>
        </div>
        {diagnostic.detail && (
          <div className="text-neutral-600 dark:text-slate-400">
            {diagnostic.detail}
          </div>
        )}
        {diagnostic.hint && (
          <div className="text-xs text-neutral-500 dark:text-slate-500">
            {diagnostic.hint}
          </div>
        )}
        {diagnostic.pending && (
          <div className="mt-1 flex flex-col gap-1 text-neutral-600 dark:text-slate-400">
            <RotateThrough interval={7 * 1000}>
              {WHIMSICAL_LOADING_MESSAGES.map((message) => (
                <span key={message}>{message}</span>
              ))}
            </RotateThrough>
            <AnimatedWave text={WHIMSICAL_SPARKLE} />
          </div>
        )}
        {children}
      </div>
    </div>
  );
}

export { SCREENSHOT_PANEL_CLASS, StreamModeBadge, StreamStatusPanel };
export type { StreamDiagnostic, StreamMode };
