import { useEffect, useState } from "react";

import { VERB_CYCLE_MS, pickWorkingVerb } from "./workingVerbs";

type Props = {
  queued: boolean;
};

export function CopilotWorkingStatus({ queued }: Props) {
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
    </div>
  );
}
