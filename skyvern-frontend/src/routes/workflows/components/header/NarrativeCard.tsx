import { cn } from "@/util/utils";

type Props = {
  index: number;
  description: string;
};

const VARIANTS = {
  1: {
    cardBg: "bg-brand-soft/60 dark:bg-brand/10",
    chipBg: "bg-brand-cta text-brand-cta-foreground",
  },
  2: {
    cardBg: "bg-warning/10 dark:bg-warning/15",
    chipBg: "bg-warning text-warning-foreground",
  },
  3: {
    cardBg: "bg-success/10 dark:bg-success/15",
    chipBg: "bg-success text-success-foreground",
  },
} as const;

function NarrativeCard({ index, description }: Props) {
  const variant = VARIANTS[index as 1 | 2 | 3] ?? VARIANTS[1];

  return (
    <div
      className={cn(
        "flex h-32 w-52 flex-col gap-3 rounded-xl border border-border p-4 shadow-sm",
        variant.cardBg,
      )}
    >
      <div
        className={cn(
          "flex size-6 items-center justify-center rounded-full text-xs font-semibold shadow-sm",
          variant.chipBg,
        )}
      >
        {index}
      </div>
      <div className="text-sm text-foreground/80">{description}</div>
    </div>
  );
}

export { NarrativeCard };
