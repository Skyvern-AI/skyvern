import type { ComponentType, ReactNode } from "react";
import { cn } from "@/util/utils";

type Props = {
  icon?: ComponentType<{ className?: string }>;
  label: string;
  description?: string;
  meta?: ReactNode;
  selected?: boolean;
  disabled?: boolean;
  onClick: () => void;
};

function OnboardingOptionRow(props: Readonly<Props>) {
  const {
    icon: Icon,
    label,
    description,
    meta,
    selected,
    disabled,
    onClick,
  } = props;
  return (
    <button
      type="button"
      aria-pressed={selected}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex min-h-[3.5rem] w-full touch-manipulation flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border px-4 py-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background motion-reduce:transition-none",
        selected
          ? "border-2 border-primary bg-primary/5 px-[15px] py-[11px]"
          : "border-border hover:border-primary/60 hover:bg-muted/50",
        disabled && "pointer-events-none opacity-50",
      )}
    >
      {Icon ? (
        <span aria-hidden="true" className="shrink-0">
          <Icon className="h-5 w-5 text-primary" />
        </span>
      ) : null}
      <div className="min-w-0 flex-1 basis-[calc(100%-2rem)] sm:basis-0">
        <p className="truncate text-sm font-medium">{label}</p>
        {description ? (
          <p className="line-clamp-2 text-xs text-muted-foreground">
            {description}
          </p>
        ) : null}
      </div>
      {meta ? (
        <span className="basis-full pl-8 text-xs tabular-nums text-muted-foreground sm:ml-auto sm:basis-auto sm:pl-0 sm:text-right">
          {meta}
        </span>
      ) : null}
    </button>
  );
}

export { OnboardingOptionRow };
