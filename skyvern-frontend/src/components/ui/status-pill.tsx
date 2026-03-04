import * as React from "react";
import { type VariantProps } from "class-variance-authority";
import { cn } from "@/util/utils";
import { statusPillVariants } from "./status-pill-variants";

type StatusPillProps = React.HTMLAttributes<HTMLDivElement> &
  VariantProps<typeof statusPillVariants> & {
    icon?: React.ReactNode;
  };

const StatusPill = React.forwardRef<HTMLDivElement, StatusPillProps>(
  ({ icon, variant, className, children, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(statusPillVariants({ variant }), className)}
        {...props}
      >
        {icon}
        {children != null && <span className="text-xs">{children}</span>}
      </div>
    );
  },
);
StatusPill.displayName = "StatusPill";

export { StatusPill };
