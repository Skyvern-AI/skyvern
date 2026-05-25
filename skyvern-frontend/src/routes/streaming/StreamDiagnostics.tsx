import type { ReactNode } from "react";
import { InfoCircledIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";

type StreamDiagnostic = {
  title: string;
  detail?: string;
  hint?: string;
};

type StreamMode = "cdp" | "vnc" | "fallback" | "unavailable";

const STREAM_MODE_COPY: Record<
  StreamMode,
  { label: string; title: string; className: string }
> = {
  cdp: {
    label: "Local stream",
    title: "Local browser streaming through the backend",
    className: "border-cyan-500/40 bg-cyan-500/10 text-cyan-200",
  },
  vnc: {
    label: "VNC",
    title: "VNC browser streaming",
    className: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200",
  },
  fallback: {
    label: "VNC -> Local",
    title: "VNC disconnected; using local browser streaming fallback",
    className: "border-amber-500/40 bg-amber-500/10 text-amber-200",
  },
  unavailable: {
    label: "Unavailable",
    title: "Browser streaming is unavailable for this session",
    className: "border-border bg-muted text-muted-foreground",
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
        "flex h-full w-full items-center justify-center rounded-md border bg-slate-elevation1 p-6 text-muted-foreground",
        className,
      )}
    >
      <div className="flex max-w-md flex-col gap-2 text-sm">
        <div className="flex items-center gap-2 font-medium text-foreground">
          <InfoCircledIcon className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
          <span>{diagnostic.title}</span>
        </div>
        {diagnostic.detail && (
          <div className="text-muted-foreground">{diagnostic.detail}</div>
        )}
        {diagnostic.hint && (
          <div className="text-xs text-muted-foreground/70">
            {diagnostic.hint}
          </div>
        )}
        {children}
      </div>
    </div>
  );
}

export { StreamModeBadge, StreamStatusPanel };
export type { StreamDiagnostic, StreamMode };
