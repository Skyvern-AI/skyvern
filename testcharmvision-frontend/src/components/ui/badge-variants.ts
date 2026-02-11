import { cva } from "class-variance-authority";

const badgeVariants = cva(
  "inline-flex items-center rounded-md border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-primary text-primary-foreground shadow hover:bg-primary/80",
        secondary:
          "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80",
        success:
          "border-transparent bg-success/40 text-success-foreground shadow hover:bg-success/30",
        warning:
          "border-transparent bg-warning/40 text-warning-foreground shadow hover:bg-warning/30",
        destructive:
          "border-transparent bg-destructive/40 text-destructive-foreground shadow hover:bg-destructive/30",
        outline: "text-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export { badgeVariants };
