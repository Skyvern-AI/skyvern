import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { badgeVariants } from "@/components/ui/badge-variants";
import { cn } from "@/util/utils";
import { TagChip } from "./TagChip";

type Props = {
  tags: Record<string, string>;
  // Map (not a plain object) so lookups by user-controlled tag keys can't hit
  // inherited Object prototype members (e.g. a key named "constructor").
  descriptions?: Map<string, string | null>;
  maxVisible?: number;
  className?: string;
};

// Generic list of tag chips with a "+N" overflow affordance. Sorts by key for
// stable ordering across renders. Reusable for any key->value tag map.
function TagChipList({ tags, descriptions, maxVisible = 3, className }: Props) {
  const entries = Object.entries(tags).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) {
    return null;
  }

  const visible = entries.slice(0, maxVisible);
  const hidden = entries.slice(maxVisible);

  return (
    <div className={cn("flex flex-wrap items-center gap-1", className)}>
      {visible.map(([key, value]) => (
        <TagChip
          key={key}
          tagKey={key}
          value={value}
          description={descriptions?.get(key)}
        />
      ))}
      {hidden.length > 0 ? (
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                className={cn(
                  badgeVariants({ variant: "outline" }),
                  "cursor-default font-normal text-muted-foreground",
                )}
              >
                +{hidden.length}
              </span>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs">
              <div className="flex flex-col gap-1">
                {hidden.map(([key, value]) => (
                  <span key={key} className="break-words">
                    <span className="font-medium">{key}</span>: {value}
                  </span>
                ))}
              </div>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      ) : null}
    </div>
  );
}

export { TagChipList };
