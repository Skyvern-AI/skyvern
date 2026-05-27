import { cva } from "class-variance-authority";

const statusPillVariants = cva("flex items-center gap-1 rounded-sm px-2 py-1", {
  variants: {
    variant: {
      neutral:
        "bg-neutral-200 text-neutral-800 dark:bg-slate-elevation5 dark:text-foreground",
    },
  },
  defaultVariants: {
    variant: "neutral",
  },
});

export { statusPillVariants };
