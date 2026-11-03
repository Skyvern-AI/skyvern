import { Fragment, useContext } from "react";
import { cn } from "@/util/utils";
import { splitJinjaReferences } from "./docTextUtils";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

/** Plain text with each {{ reference }} shown as a grey pill. */
export function DocText({
  value,
  className,
}: {
  value: string;
  className?: string;
}) {
  const { missingReference } = useContext(WorkflowDocActionsContext);
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
            title={
              missingReference(part.ref)
                ? `{{ ${part.ref} }}: missing reference`
                : `{{ ${part.ref} }}`
            }
            className={cn(
              "inline-block max-w-full rounded bg-muted px-[5px] font-[520] leading-[1.25] text-foreground",
              missingReference(part.ref) &&
                "bg-destructive/10 text-destructive",
            )}
          >
            {part.ref}
          </span>
        ),
      )}
    </span>
  );
}
