import { cn } from "@/util/utils";
import { useEffect, useRef } from "react";

type RunCardStatus = "success" | "failure";

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
          "cursor-pointer hover:ring-white/25": !!onClick,
          "border-l-destructive": status === "failure",
          "border-l-success": status === "success",
          "ring-white/25": active,
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
