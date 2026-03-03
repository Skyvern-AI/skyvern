import { cva } from "class-variance-authority";

const statusPillVariants = cva("flex items-center gap-1 rounded-sm px-2 py-1", {
  variants: {
    variant: {
      neutral: "bg-slate-elevation5",
    },
  },
  defaultVariants: {
    variant: "neutral",
  },
});

export { statusPillVariants };
