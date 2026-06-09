import { cva } from "class-variance-authority";

const badgeVariants = cva(
  "inline-flex items-center whitespace-nowrap rounded-md border px-2.5 py-1 text-xs font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-primary text-primary-foreground shadow hover:bg-primary/80",
        secondary: "border-transparent bg-badge-neutral text-foreground",
        success: "border-transparent bg-badge-success text-foreground",
        warning: "border-transparent bg-badge-warning text-foreground",
        destructive: "border-transparent bg-badge-destructive text-foreground",
        terminated: "border-transparent bg-badge-terminated text-foreground",
        outline: "text-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export { badgeVariants };
