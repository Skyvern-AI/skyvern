import { useEffect, useState } from "react";

import { SparklesIcon } from "@/components/icons/SparklesIcon";
import { cn } from "@/util/utils";

import { VERB_CYCLE_MS, pickWorkingVerb } from "./workingVerbs";

export type CopilotComposerStatus = "working" | "waiting";

const VERB_ROLL_MS = 200;

type RollPhase = "shown" | "leaving" | "entering";

type Props = {
  status: CopilotComposerStatus;
  queued: boolean;
};

export function CopilotWorkingStatus({ status, queued }: Props) {
  const [verb, setVerb] = useState(() => pickWorkingVerb());
  const [phase, setPhase] = useState<RollPhase>("shown");

  useEffect(() => {
    if (status !== "working") return;
    let swap: ReturnType<typeof setTimeout> | undefined;
    let frame: number | undefined;
    const timer = setInterval(() => {
      setPhase("leaving");
      swap = setTimeout(() => {
        setVerb((previous) => pickWorkingVerb(previous));
        setPhase("entering");
        // Two frames so the below-the-line start position paints before the
        // transition back to rest; one frame can coalesce with the swap.
        frame = requestAnimationFrame(() => {
          frame = requestAnimationFrame(() => setPhase("shown"));
        });
      }, VERB_ROLL_MS);
    }, VERB_CYCLE_MS);
    return () => {
      clearInterval(timer);
      clearTimeout(swap);
      if (frame !== undefined) cancelAnimationFrame(frame);
      setPhase("shown");
    };
  }, [status]);

  const announcement =
    status === "waiting"
      ? "Waiting for you"
      : queued
        ? "Message queued"
        : "Working";

  return (
    <div
      // While waiting, the question tray's header is the visible cue. The live region stays
      // mounted so the switch from working to waiting is still announced.
      className={cn(
        "flex items-center pl-0.5 text-[12.5px]",
        status === "working" ? "mb-3 min-h-6" : "sr-only",
      )}
      data-testid="copilot-working-status"
    >
      {status === "working" ? (
        <span aria-hidden className="flex min-w-0 items-center gap-2">
          {/* top-px: the text's glyphs sit ~1px below its line box center, which is where
              items-center puts the markers; `top` leaves the breathe transform alone. */}
          <SparklesIcon className="relative top-px h-3.5 w-3.5 shrink-0 animate-copilot-sparkle-breathe text-blue-600 motion-reduce:animate-none dark:text-blue-300" />
          <span
            className={cn(
              "min-w-0 truncate text-zinc-700 transition-[transform,opacity] duration-200 ease-out motion-reduce:translate-y-0 motion-reduce:opacity-100 motion-reduce:transition-none dark:text-slate-300",
              phase === "leaving" && "-translate-y-[7px] opacity-0",
              phase === "entering" &&
                "translate-y-[7px] opacity-0 transition-none",
            )}
          >
            {verb}…
          </span>
        </span>
      ) : null}
      <span className="sr-only" aria-live="polite">
        {announcement}
      </span>
    </div>
  );
}
