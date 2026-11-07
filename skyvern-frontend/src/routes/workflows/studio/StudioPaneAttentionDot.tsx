import { cn } from "@/util/utils";

/** Pulsing corner dot on a pane control: new Browser activity, or Copilot waiting on the person. */
export function StudioPaneAttentionDot({
  tone,
  title,
}: {
  tone: "activity" | "waiting";
  title: string;
}) {
  const color = tone === "activity" ? "bg-primary" : "bg-amber-500";
  return (
    <span
      aria-hidden
      title={title}
      className="absolute -right-0.5 -top-0.5 flex size-2"
    >
      <span
        className={cn(
          "absolute inline-flex h-full w-full rounded-full opacity-75 motion-safe:animate-ping",
          color,
        )}
      />
      <span className={cn("relative inline-flex size-2 rounded-full", color)} />
    </span>
  );
}
