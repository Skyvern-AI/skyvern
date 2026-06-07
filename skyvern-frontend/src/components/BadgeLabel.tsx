import type { ReactNode } from "react";

import { cn } from "@/util/utils";

export type BadgeVariant = "default" | "success" | "warning";

type Props = {
  label: ReactNode;
  badge?: string;
  badgeVariant?: BadgeVariant;
  className?: string;
};

function BadgeLabel({
  label,
  badge,
  badgeVariant = "default",
  className,
}: Props) {
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <span>{label}</span>
      {badge && (
        <span
          className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium", {
            "bg-green-500/20 text-green-400": badgeVariant === "success",
            "bg-amber-500/20 text-amber-400": badgeVariant === "warning",
            "bg-slate-500/20 text-slate-400": badgeVariant === "default",
          })}
        >
          {badge}
        </span>
      )}
    </div>
  );
}

export { BadgeLabel };
