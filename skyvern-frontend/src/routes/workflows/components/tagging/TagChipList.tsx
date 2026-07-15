import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { badgeVariants } from "@/components/ui/badge-variants";
import { cn } from "@/util/utils";
import {
  normalizeWorkflowTags,
  isSystemTagKey,
  isUserWritableTagKey,
  sortTags,
  tagElementKey,
  type Tag,
} from "../../types/tagTypes";
import { tagColorFor, type TagColorMap } from "../../types/tagColors";
import { TagChip } from "./TagChip";

type Props = {
  tags: Array<Tag>;
  // Group (key) -> description. A Map (not a plain object) so user-controlled keys
  // can't hit Object prototype members (e.g. a key named "constructor").
  descriptions?: Map<string, string | null>;
  // (key, value) -> palette color for grouped tags. Standalone labels stay neutral.
  colors?: TagColorMap;
  maxVisible?: number;
  // Drop reserved skyvern.* tags entirely — for table rows, where system
  // metadata duplicates real columns (e.g. skyvern.status vs the STATUS cell).
  hideSystemTags?: boolean;
  // Row-density styling: single line, smaller quieter chips.
  compact?: boolean;
  className?: string;
  onRemove?: (tag: Tag) => void;
};

const COMPACT_CHIP_CLASS = "h-5 max-w-40 px-1.5 py-0";
const SYSTEM_CHIP_CLASS = "border-border bg-transparent text-muted-foreground";

// Generic list of tag chips with a "+N" overflow affordance. Standalone labels
// sort first, then grouped by key, for stable ordering across renders.
function TagChipList({
  tags,
  descriptions,
  colors,
  maxVisible = 3,
  hideSystemTags = false,
  compact = false,
  className,
  onRemove,
}: Props) {
  // Re-validate at render time: callers feed this straight from API payloads,
  // and a shape skew here previously killed the whole route via React #31.
  const safeTags = normalizeWorkflowTags(tags).filter(
    (tag) => !hideSystemTags || isUserWritableTagKey(tag.key),
  );
  if (safeTags.length === 0) {
    return null;
  }
  // System tags sort after user tags (stable within each group) so user
  // labels keep the visible slots.
  const sorted = sortTags(safeTags).sort(
    (a, b) => Number(isSystemTagKey(a.key)) - Number(isSystemTagKey(b.key)),
  );
  const visible = sorted.slice(0, maxVisible);
  const hidden = sorted.slice(maxVisible);

  return (
    <div
      className={cn(
        "flex items-center gap-1",
        compact ? "flex-nowrap" : "flex-wrap",
        className,
      )}
    >
      {visible.map((tag) => (
        <TagChip
          key={tagElementKey(tag)}
          tagKey={tag.key}
          value={tag.value}
          description={
            tag.key !== null ? descriptions?.get(tag.key) : undefined
          }
          color={tagColorFor(colors, tag.key, tag.value)}
          className={cn(
            compact && COMPACT_CHIP_CLASS,
            isSystemTagKey(tag.key) && SYSTEM_CHIP_CLASS,
          )}
          onRemove={
            onRemove && isUserWritableTagKey(tag.key)
              ? () => onRemove(tag)
              : undefined
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
                  compact && COMPACT_CHIP_CLASS,
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
