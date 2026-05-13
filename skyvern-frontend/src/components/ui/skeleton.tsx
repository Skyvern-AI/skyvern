import * as React from "react";
import type { VariantProps } from "class-variance-authority";

import { cn } from "@/util/utils";
import { skeletonVariants } from "./skeleton-variants";

type SkeletonBaseProps = React.HTMLAttributes<HTMLDivElement> &
  VariantProps<typeof skeletonVariants>;

interface SkeletonProps extends SkeletonBaseProps {
  size?: number;
  lines?: number;
}

function Skeleton({
  className,
  variant,
  size,
  lines,
  style,
  ...props
}: SkeletonProps) {
  if (variant === "circle") {
    const dimension = size ?? 24;
    return (
      <div
        className={cn(skeletonVariants({ variant }), className)}
        style={{ width: dimension, height: dimension, ...style }}
        {...props}
      />
    );
  }

  if (variant === "text") {
    const lineCount = Math.max(1, lines ?? 1);
    return (
      <div
        className={cn(skeletonVariants({ variant }), className)}
        style={style}
        {...props}
      >
        {Array.from({ length: lineCount }).map((_, i) => (
          <div
            key={i}
            data-skeleton-line=""
            className={cn(
              "h-4 animate-pulse rounded-md bg-primary/10",
              i === lineCount - 1 && lineCount > 1 ? "w-2/3" : "w-full",
            )}
          />
        ))}
      </div>
    );
  }

  return (
    <div
      className={cn(skeletonVariants({ variant }), className)}
      style={style}
      {...props}
    />
  );
}

export { Skeleton };
export type { SkeletonProps };
