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
      className={cn(
        "bg-slate-elevation3",
        status != null
          ? "rounded-lg border-2 border-transparent"
          : "rounded-md border",
        {
          "cursor-pointer hover:border-slate-50": !!onClick,
          "border-l-destructive": status === "failure",
          "border-l-success": status === "success",
          "border-slate-50": active,
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
