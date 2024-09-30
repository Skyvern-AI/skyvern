import { cn } from "@/util/utils";
import { useState } from "react";

type Props = {
  index: number;
  title: string;
  active: boolean;
  hasError?: boolean;
  onClick?: () => void;
  children?: React.ReactNode;
};

function TaskFormSection({
  index,
  title,
  active,
  onClick,
  children,
  hasError,
}: Props) {
  const [hovering, setHovering] = useState(false);

  return (
    <section
      className={cn("space-y-8 rounded-lg bg-slate-elevation3 px-6 py-5", {
        "cursor-pointer": !active,
      })}
      onMouseOver={() => setHovering(true)}
      onMouseOut={() => setHovering(false)}
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
      onClick={() => onClick && onClick()}
    >
      <header className="flex h-7 gap-4">
        <div
          className={cn(
            "flex w-7 items-center justify-center rounded-full border border-slate-400",
            {
              "bg-slate-400": hovering || active,
              "border-destructive": !active && hasError,
            },
          )}
        >
          <span
            className={cn("text-slate-50", {
              "text-slate-950": hovering || active,
            })}
          >
            {String(index)}
          </span>
        </div>
        <span
          className={cn("text-lg", {
            "text-destructive": !active && hasError,
          })}
        >
          {title}
        </span>
      </header>
      {children}
    </section>
  );
}

export { TaskFormSection };
