import { cva } from "class-variance-authority";

/** Default tone preserves pre-cva Card output. Non-default tones swap only the border color (border-tint, no background wash). */
const cardVariants = cva(
  "rounded-xl border bg-card text-card-foreground shadow",
  {
    variants: {
      tone: {
        default: "",
        success: "border-success/40",
        warning: "border-warning/40",
        destructive: "border-destructive/40",
      },
    },
    defaultVariants: {
      tone: "default",
    },
  },
);

export { cardVariants };
