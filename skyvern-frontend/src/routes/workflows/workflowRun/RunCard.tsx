import { cn } from "@/util/utils";
import { useEffect, useRef } from "react";

import { terminatedBorder } from "@/components/terminatedVisual";

type RunCardStatus = "success" | "failure" | "terminated";

type RunCardProps = {
  active?: boolean;
  status?: RunCardStatus;
  onClick?: React.MouseEventHandler<HTMLDivElement>;
  className?: string;
  children: React.ReactNode;
};

function RunCard({
  active,
  status,
  onClick,
  className,
  children,
}: RunCardProps) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (ref.current && active) {
      ref.current.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
  }, [active]);

  return (
    <div
      data-slot="runcard"
      className={cn(
        "rounded-lg bg-slate-elevation4 ring-1 ring-transparent transition-all duration-200",
        status != null && "border-l-2 border-l-transparent",
        {
          "cursor-pointer hover:ring-neutral-400/40 dark:hover:ring-white/25":
            !!onClick,
          "border-l-destructive": status === "failure" && !active,
          "border-l-success": status === "success" && !active,
          [terminatedBorder]: status === "terminated" && !active,
          "ring-2 ring-neutral-500/45 hover:ring-neutral-500/45 dark:ring-white/55 dark:hover:ring-white/55":
            active,
        },
        className,
      )}
      onClick={onClick}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                e.currentTarget.click();
              }
            }
          : undefined
      }
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      ref={ref}
    >
      {children}
    </div>
  );
}

export { RunCard };
export type { RunCardProps, RunCardStatus };
