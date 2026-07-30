import { CodeIcon } from "@radix-ui/react-icons";

import { cn } from "@/util/utils";

type Props = {
  pressed: boolean;
  pickerTitle: string;
  onToggle: (enabled: boolean) => void;
};

function TemplateModeToggle({ pressed, pickerTitle, onToggle }: Props) {
  const label = pressed ? pickerTitle : "Enter a custom value";

  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={pressed}
      title={label}
      className={cn(
        "rounded p-1 text-muted-foreground transition-colors hover:text-foreground dark:hover:text-slate-200",
        pressed && "bg-muted text-foreground dark:bg-slate-700",
      )}
      onClick={() => onToggle(!pressed)}
    >
      <CodeIcon className="size-4" />
    </button>
  );
}

export { TemplateModeToggle };
