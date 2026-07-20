import { cn } from "@/util/utils";

function WorkflowChangesList({
  changes,
  className,
}: {
  changes: Array<string>;
  className?: string;
}) {
  if (changes.length === 0) {
    return null;
  }
  return (
    <ul
      className={cn(
        "max-h-48 space-y-1.5 overflow-y-auto rounded-md border border-border bg-slate-elevation3 p-3 text-sm text-foreground",
        className,
      )}
    >
      {changes.map((change, index) => (
        <li key={index} className="flex gap-2">
          <span className="select-none text-muted-foreground">&bull;</span>
          <span className="min-w-0 break-words">{change}</span>
        </li>
      ))}
    </ul>
  );
}

export { WorkflowChangesList };
