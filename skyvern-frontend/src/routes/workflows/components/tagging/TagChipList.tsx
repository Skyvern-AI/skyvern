import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { badgeVariants } from "@/components/ui/badge-variants";
import { cn } from "@/util/utils";
import { sortTags, tagElementKey, type Tag } from "../../types/tagTypes";
import { TagChip } from "./TagChip";

type Props = {
  tags: Array<Tag>;
  // Group (key) -> description. A Map (not a plain object) so user-controlled keys
  // can't hit Object prototype members (e.g. a key named "constructor").
  descriptions?: Map<string, string | null>;
  maxVisible?: number;
  className?: string;
};

// Generic list of tag chips with a "+N" overflow affordance. Standalone labels
// sort first, then grouped by key, for stable ordering across renders.
function TagChipList({ tags, descriptions, maxVisible = 3, className }: Props) {
  if (tags.length === 0) {
    return null;
  }
  const sorted = sortTags(tags);
  const visible = sorted.slice(0, maxVisible);
  const hidden = sorted.slice(maxVisible);

  return (
    <div className={cn("flex flex-wrap items-center gap-1", className)}>
      {visible.map((tag) => (
        <TagChip
          key={tagElementKey(tag)}
          tagKey={tag.key}
          value={tag.value}
          description={
            tag.key !== null ? descriptions?.get(tag.key) : undefined
          }
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
                {hidden.map((tag) => (
                  <span key={tagElementKey(tag)} className="break-words">
                    {tag.key !== null ? (
                      <>
                        <span className="font-medium">{tag.key}</span>:{" "}
                      </>
                    ) : null}
                    {tag.value}
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
