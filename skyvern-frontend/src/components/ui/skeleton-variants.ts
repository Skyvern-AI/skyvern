import { cva } from "class-variance-authority";

/** Default `rect` variant renders byte-identical to the pre-cva Skeleton. */
const skeletonVariants = cva("animate-pulse rounded-md bg-primary/10", {
  variants: {
    variant: {
      rect: "",
      // tailwind-merge collapses rounded-md (base) → rounded-full (variant).
      circle: "rounded-full",
      // Container only — line bars are rendered as children by the component.
      text: "flex flex-col gap-2 bg-transparent rounded-none animate-none",
    },
  },
  defaultVariants: {
    variant: "rect",
  },
});

export { skeletonVariants };
