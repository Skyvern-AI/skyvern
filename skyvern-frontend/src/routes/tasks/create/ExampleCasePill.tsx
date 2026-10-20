import { CheckIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";

type Props = {
  icon: React.ReactNode;
  label: string;
  hint?: string;
  selected?: boolean;
  onClick: () => void;
  disabled?: boolean;
};

function ExampleCasePill({
  icon,
  label,
  hint,
  selected = false,
  onClick,
  disabled = false,
}: Props) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex min-h-16 w-full items-center gap-3 rounded-xl border px-3.5 py-3 text-left text-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:pointer-events-none disabled:opacity-50",
        selected
          ? "border-indigo-600/50 bg-indigo-600/5 dark:border-indigo-300/55 dark:bg-indigo-300/[0.07]"
          : "border-border/70 bg-slate-elevation1 hover:bg-slate-elevation2",
      )}
    >
      <span
        aria-hidden="true"
        className="flex size-9 shrink-0 items-center justify-center rounded-[9px] bg-indigo-600/[0.08] text-indigo-600 dark:bg-indigo-300/10 dark:text-indigo-300 [&_svg]:size-[18px]"
      >
        {icon}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-sm font-medium leading-5">{label}</span>
        {hint ? (
          <span className="truncate text-xs text-muted-foreground">{hint}</span>
        ) : null}
      </span>
      {selected ? (
        <CheckIcon
          aria-hidden="true"
          className="size-4 shrink-0 text-indigo-600 dark:text-indigo-300"
        />
      ) : null}
    </button>
  );
}

export { ExampleCasePill };
