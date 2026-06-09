import { Cross2Icon } from "@radix-ui/react-icons";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { badgeVariants } from "@/components/ui/badge-variants";
import { cn } from "@/util/utils";

type Props = {
  // null = a standalone label (no group); rendered as just the value.
  tagKey: string | null;
  value: string;
  description?: string | null;
  onRemove?: () => void;
  className?: string;
};

// Generic single-tag chip: a styled span (not Badge div) so it can be a Radix
// TooltipTrigger `asChild` target. Grouped shows `key: value`, standalone the value.
function TagChip({ tagKey, value, description, onRemove, className }: Props) {
  const label = tagKey === null ? value : `${tagKey}: ${value}`;
  const chip = (
    <span
      className={cn(
        badgeVariants({ variant: "secondary" }),
        "max-w-full gap-1 font-normal",
        className,
      )}
    >
      <span className="truncate">
        {tagKey !== null ? (
          <>
            <span className="font-medium">{tagKey}</span>
            <span className="text-muted-foreground">: </span>
          </>
        ) : null}
        {value}
      </span>
      {onRemove ? (
        <button
          type="button"
          aria-label={`Remove ${label}`}
          className="ml-0.5 shrink-0 rounded-sm opacity-70 hover:opacity-100"
          onClick={(event) => {
            event.stopPropagation();
            onRemove();
          }}
        >
          <Cross2Icon className="h-3 w-3" />
        </button>
      ) : null}
    </span>
  );

  if (!description) {
    return chip;
  }

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>{chip}</TooltipTrigger>
        <TooltipContent className="max-w-xs break-words">
          {description}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { TagChip };
