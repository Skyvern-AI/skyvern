import { ReactNode } from "react";
import { cn } from "@/util/utils";
import { HighlightText } from "./HighlightText";

type ParameterDisplayItem = {
  key: string;
  value: unknown;
  description?: string | null;
};

type ParameterDisplayInlineProps = {
  title?: string;
  parameters: Array<ParameterDisplayItem>;
  searchQuery: string;
  keywordMatchesParameter: (parameter: ParameterDisplayItem) => boolean;
  showDescription?: boolean;
  emptyMessage?: ReactNode;
  className?: string;
};

function getDisplayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }

  if (typeof value === "string") {
    return value;
  }

  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function ParameterDisplayInline({
  title = "Parameters",
  parameters,
  searchQuery,
  keywordMatchesParameter,
  showDescription = true,
  emptyMessage = "No parameters for this run",
  className,
}: ParameterDisplayInlineProps) {
  if (!parameters || parameters.length === 0) {
    return (
      <div className={cn("ml-8 py-4 text-sm text-slate-400", className)}>
        {emptyMessage}
      </div>
    );
  }

  return (
    <div className={cn("ml-8 space-y-2 py-4", className)}>
      <div className="mb-3 text-sm font-medium">{title}</div>
      <div className="space-y-2">
        {parameters.map((parameter) => {
          const displayValue = getDisplayValue(parameter.value);
          const matches = keywordMatchesParameter(parameter);

          return (
            <div
              key={parameter.key}
              className={cn(
                "grid gap-6 rounded border bg-white p-3 text-sm dark:border-slate-800 dark:bg-slate-900",
                showDescription
                  ? "grid-cols-[minmax(200px,1fr)_minmax(200px,1fr)_minmax(300px,2fr)]"
                  : "grid-cols-[minmax(200px,1fr)_minmax(300px,2fr)]",
                matches &&
                  "shadow-[0_0_15px_rgba(59,130,246,0.3)] ring-2 ring-blue-500/50",
              )}
            >
              <div className="font-medium text-blue-600 dark:text-blue-400">
                <HighlightText text={parameter.key} query={searchQuery} />
              </div>
              <div className="truncate">
                {displayValue === "-" ? (
                  <span className="text-slate-400">-</span>
                ) : (
                  <HighlightText text={displayValue} query={searchQuery} />
                )}
              </div>
              {showDescription ? (
                <div className="text-slate-500">
                  {parameter.description ? (
                    <HighlightText
                      text={parameter.description}
                      query={searchQuery}
                    />
                  ) : (
                    <span className="text-slate-400">No description</span>
                  )}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export type { ParameterDisplayItem };
export { ParameterDisplayInline };
