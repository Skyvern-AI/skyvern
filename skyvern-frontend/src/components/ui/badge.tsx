import * as React from "react";
import { ReloadIcon } from "@radix-ui/react-icons";
import { type VariantProps } from "class-variance-authority";

import { cn } from "@/util/utils";
import { badgeVariants } from "./badge-variants";

export interface BadgeProps
  extends
    React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, children, ...props }: BadgeProps) {
  return (
    <div className={cn(badgeVariants({ variant }), className)} {...props}>
      {variant === "progress" ? (
        <ReloadIcon
          aria-hidden
          className="size-[1em] shrink-0 motion-safe:animate-spin"
        />
      ) : null}
      {children}
    </div>
  );
}

export { Badge };
