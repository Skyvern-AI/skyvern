import { CodeIcon, EyeOpenIcon } from "@radix-ui/react-icons";

import { cn } from "@/util/utils";

const segmentClass = (active: boolean) =>
  cn(
    "flex items-center gap-1 rounded px-2 py-1 text-xs transition-colors",
    active
      ? "bg-slate-elevation3 text-slate-100"
      : "text-slate-400 hover:text-slate-200",
  );

type Props = {
  mode: "visual" | "code";
  onVisual?: () => void;
  onCode?: () => void;
  disabled?: boolean;
};

// Segmented Visual/Code toggle for the workflow editor. "Code" opens the
// full-screen YAML editor; "Visual" commits the YAML back into the canvas.
export function YamlModeToggle({ mode, onVisual, onCode, disabled }: Props) {
  return (
    <div className="flex items-center gap-0.5 rounded-md border border-slate-700 bg-slate-elevation1 p-0.5">
      {/* The active segment stays focusable (aria-disabled, not disabled) so
          keyboard users can tab to it and hear the pressed state. */}
      <button
        type="button"
        aria-pressed={mode === "visual"}
        aria-disabled={mode === "visual" || undefined}
        disabled={disabled}
        onClick={mode === "visual" ? undefined : onVisual}
        className={segmentClass(mode === "visual")}
      >
        <EyeOpenIcon className="size-3" />
        Visual
      </button>
      <button
        type="button"
        aria-pressed={mode === "code"}
        aria-disabled={mode === "code" || undefined}
        disabled={disabled}
        onClick={mode === "code" ? undefined : onCode}
        className={segmentClass(mode === "code")}
      >
        <CodeIcon className="size-3" />
        Code
      </button>
    </div>
  );
}
