import { type ReactNode } from "react";

export function OverviewField({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <div className="break-words text-sm text-foreground">{children}</div>
    </div>
  );
}
