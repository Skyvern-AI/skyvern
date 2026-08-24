import { Pencil1Icon } from "@radix-ui/react-icons";
import { useEffect, useState } from "react";

import { VERB_CYCLE_MS, pickWorkingVerb } from "./workingVerbs";

type Props = {
  queued: boolean;
  onDismissQueued: () => void;
};

export function CopilotWorkingStatus({ queued, onDismissQueued }: Props) {
  const [verb, setVerb] = useState(() => pickWorkingVerb());

  useEffect(() => {
    const timer = setInterval(
      () => setVerb((previous) => pickWorkingVerb(previous)),
      VERB_CYCLE_MS,
    );
    return () => clearInterval(timer);
  }, []);

  return (
    <div
      className="mb-2 flex items-center gap-2 pl-0.5"
      data-testid="copilot-working-status"
    >
      <span
        key={verb}
        aria-hidden
        className="min-w-0 truncate duration-150 animate-in fade-in"
      >
        <span className="animate-copilot-verb-shimmer bg-[linear-gradient(90deg,#6B7688_0%,#EAF3FF_50%,#6B7688_100%)] bg-[length:220%_100%] bg-clip-text text-[13.5px] font-semibold text-transparent motion-reduce:animate-none motion-reduce:bg-none motion-reduce:text-[#9AA5B6]">
          {verb}…
        </span>
      </span>
      <span className="sr-only" aria-live="polite">
        {queued ? "Message queued" : "Working"}
      </span>
      {queued ? (
        <div className="ml-auto flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full bg-slate-400/[0.12] py-0.5 pl-2.5 pr-1 text-xs text-muted-foreground">
          1 message queued
          {/* A cross here would read as "cancel the queued message"; this
              returns it to the composer for editing. */}
          <button
            type="button"
            onClick={onDismissQueued}
            title="Edit queued message"
            aria-label="Edit queued message"
            className="rounded px-1 hover:text-accent-foreground"
          >
            <Pencil1Icon className="h-3 w-3" />
          </button>
        </div>
      ) : null}
    </div>
  );
}
