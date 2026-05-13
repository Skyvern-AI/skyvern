import { cva } from "class-variance-authority";

/** Default `rect` variant renders byte-identical to the pre-cva Skeleton. */
const skeletonVariants = cva("animate-pulse rounded-md bg-primary/10", {
  variants: {
    variant: {
      rect: "",
      circle: "rounded-full",
      text: "flex flex-col gap-2 bg-transparent rounded-none animate-none",
    },
  },
  defaultVariants: {
    variant: "rect",
  },
});

export { skeletonVariants };
