import type { ReactNode } from "react";

function LightbulbIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-3.5 w-3.5"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth="2"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 18h6m-5 3h4m-7.5-9.5a5.5 5.5 0 1 1 9.7 3.55c-.73.85-1.2 1.65-1.2 2.45H9c0-.8-.47-1.6-1.2-2.45A5.48 5.48 0 0 1 6.5 11.5Z"
      />
    </svg>
  );
}

export function SuggestionCard({
  title,
  description,
  detail,
  actions,
}: {
  title: string;
  description: string;
  detail?: ReactNode;
  actions: ReactNode;
}) {
  return (
    <section
      aria-label={`Suggestion: ${title}`}
      role="note"
      className="min-w-0 overflow-hidden rounded-lg border border-amber-500/35 bg-amber-500/[0.04]"
    >
      <div className="flex items-center gap-2 border-b border-amber-500/25 bg-amber-500/[0.08] px-3 py-2">
        <span className="flex size-4 shrink-0 items-center justify-center text-amber-700 dark:text-amber-300">
          <LightbulbIcon />
        </span>
        <span className="text-xs font-semibold text-amber-800 dark:text-amber-200">
          Suggestion
        </span>
        <span className="ml-auto text-[11px] text-amber-700 dark:text-amber-300">
          Optional
        </span>
      </div>
      <div className="space-y-2.5 px-3 py-3">
        <div>
          <div className="text-[13px] font-semibold text-foreground">
            {title}
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
            {description}
          </p>
        </div>
        {detail}
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2 border-t border-amber-500/20 px-3 py-2">
        {actions}
      </div>
    </section>
  );
}
