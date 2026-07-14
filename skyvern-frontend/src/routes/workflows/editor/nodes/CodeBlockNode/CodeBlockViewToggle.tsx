import { CodeIcon } from "@radix-ui/react-icons";

import { cn } from "@/util/utils";

export type CodeBlockView = "plain" | "code";

type Props = {
  value: CodeBlockView;
  onChange: (value: CodeBlockView) => void;
};

const segmentClass = (active: boolean) =>
  cn(
    "flex items-center gap-1 rounded px-2 py-0.5 text-xs transition-colors",
    active
      ? "bg-slate-elevation3 text-foreground"
      : "text-muted-foreground hover:text-foreground dark:hover:text-slate-200",
  );

function CodeBlockViewToggle({ value, onChange }: Props) {
  return (
    <div className="nodrag nopan flex items-center gap-0.5 rounded-md border border-border bg-slate-elevation1 p-0.5">
      <button
        type="button"
        aria-pressed={value === "plain"}
        onClick={() => onChange("plain")}
        className={segmentClass(value === "plain")}
      >
        Plain
      </button>
      <button
        type="button"
        aria-pressed={value === "code"}
        onClick={() => onChange("code")}
        className={segmentClass(value === "code")}
      >
        <CodeIcon className="size-3" />
        Code
      </button>
    </div>
  );
}

export { CodeBlockViewToggle };
