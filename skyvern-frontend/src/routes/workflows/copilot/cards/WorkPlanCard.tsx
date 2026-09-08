import { ListBulletIcon } from "@radix-ui/react-icons";

interface WorkPlanCardProps {
  items: string[];
}

export function WorkPlanCard({ items }: WorkPlanCardProps) {
  if (items.length === 0) {
    return null;
  }
  return (
    <div
      role="group"
      aria-label="Copilot's plan"
      className="overflow-hidden rounded-[10px] border border-border bg-slate-elevation2"
    >
      <div className="flex items-center gap-2 px-3 pt-3 text-xs font-semibold text-foreground">
        <ListBulletIcon className="h-3.5 w-3.5" />
        Copilot&apos;s plan
      </div>
      <ol className="max-h-[60vh] overflow-y-auto px-3 pb-3">
        {items.map((item, index) => (
          <li
            key={`${index}-${item}`}
            className="mt-2 flex gap-2 text-xs text-foreground"
          >
            <span className="shrink-0 text-muted-foreground">{index + 1}.</span>
            <span className="min-w-0 whitespace-pre-wrap break-words">
              {item}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}
