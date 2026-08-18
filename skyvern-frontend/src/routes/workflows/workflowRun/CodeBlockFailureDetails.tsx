import { ChevronDownIcon } from "@radix-ui/react-icons";

import { Badge } from "@/components/ui/badge";

import type { CodeBlockFailure } from "./codeBlockFailure";

export function CodeBlockFailureDetails({
  failure,
  reason,
}: {
  failure: CodeBlockFailure;
  reason: string | null;
}) {
  return (
    <>
      {failure.line !== null || failure.code ? (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {failure.line !== null ? (
            <Badge
              variant="secondary"
              className="h-5 px-1.5 py-0 text-[10px] tabular-nums"
            >
              Line {failure.line}
            </Badge>
          ) : null}
          {failure.code ? (
            <Badge
              variant="outline"
              className="h-5 max-w-full overflow-hidden px-1.5 py-0 text-[10px] font-normal"
              title={`Error code ${failure.code}`}
            >
              <span className="truncate font-mono">
                Error code {failure.code}
              </span>
            </Badge>
          ) : null}
        </div>
      ) : null}
      {reason ? (
        <details className="group mt-2">
          <summary className="flex w-fit cursor-pointer list-none items-center gap-1 rounded text-[11px] font-medium text-muted-foreground outline-none hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
            <ChevronDownIcon className="h-3 w-3 transition-transform group-open:rotate-180" />
            Technical details
          </summary>
          <p className="mt-1.5 max-h-32 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-elevation2 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
            {reason}
          </p>
        </details>
      ) : null}
    </>
  );
}
