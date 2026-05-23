import { type ReactNode } from "react";

import { cn } from "@/util/utils";

type Props = {
  show: boolean;
  children: ReactNode;
};

export function CollapsibleControl({ show, children }: Props) {
  return (
    <div
      aria-hidden={show ? "false" : "true"}
      className={cn(
        "flex flex-col overflow-hidden transition-all duration-300 ease-out motion-reduce:transition-none",
        show ? "max-h-9 opacity-100" : "pointer-events-none max-h-0 opacity-0",
      )}
    >
      {children}
    </div>
  );
}
