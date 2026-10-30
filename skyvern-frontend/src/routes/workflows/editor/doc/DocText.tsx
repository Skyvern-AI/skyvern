import { Fragment } from "react";
import { cn } from "@/util/utils";
import { splitJinjaReferences } from "./docTextUtils";

/** Plain text with each {{ reference }} shown as a grey pill. */
export function DocText({
  value,
  className,
}: {
  value: string;
  className?: string;
}) {
  const parts = splitJinjaReferences(value);
  return (
    <span
      className={cn("whitespace-pre-wrap [overflow-wrap:anywhere]", className)}
    >
      {parts.map((part, i) =>
        typeof part === "string" ? (
          <Fragment key={i}>{part}</Fragment>
        ) : (
          <span
            key={i}
            title={`{{ ${part.ref} }}`}
            className="inline-block max-w-full rounded bg-muted px-[5px] font-[520] leading-[1.25] text-foreground"
          >
            {part.ref}
          </span>
        ),
      )}
    </span>
  );
}
