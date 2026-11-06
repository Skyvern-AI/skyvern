import { ChevronDownIcon } from "@radix-ui/react-icons";

import type { CodeBlockFailure } from "./codeBlockFailure";

export function CodeBlockFailureDetails({
  failure,
  reason,
}: {
  failure: CodeBlockFailure;
  reason: string | null;
}) {
  const facts: Array<[string, string]> = [];
  if (failure.exceptionClass) {
    facts.push(["Exception", failure.exceptionClass]);
  }
  if (failure.line !== null) {
    facts.push(["Line", String(failure.line)]);
  }
  if (failure.sourceLine) {
    facts.push(["Source", failure.sourceLine]);
  }
  if (failure.code) {
    facts.push(["Error code", failure.code]);
  }
  if (facts.length === 0 && !reason) {
    return null;
  }
  return (
    <details className="group mt-2">
      <summary className="flex w-fit cursor-pointer list-none items-center gap-1 rounded text-[11px] font-medium text-muted-foreground outline-none hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
        <ChevronDownIcon className="h-3 w-3 transition-transform group-open:rotate-180" />
        Technical details
      </summary>
      {facts.length > 0 ? (
        <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-[11px]">
          {facts.map(([label, value]) => (
            <div key={label} className="contents">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="min-w-0 break-words font-mono">{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {reason ? (
        <p className="mt-1.5 max-h-32 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-elevation2 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
          {reason}
        </p>
      ) : null}
    </details>
  );
}
