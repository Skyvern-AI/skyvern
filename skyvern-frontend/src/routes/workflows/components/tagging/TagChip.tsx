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
  tagKey: string;
  value: string;
  description?: string | null;
  onRemove?: () => void;
  className?: string;
};

// Generic single-tag chip. Rendered as a styled span (not the Badge div) so it
// can be a Radix TooltipTrigger `asChild` target without a ref/nesting warning.
// Reusable wherever a `key: value` tag is shown (workflow cards today, run-tag
// UI later).
function TagChip({ tagKey, value, description, onRemove, className }: Props) {
  const chip = (
    <span
      className={cn(
        badgeVariants({ variant: "secondary" }),
        "max-w-full gap-1 font-normal",
        className,
      )}
    >
      <span className="truncate">
        <span className="font-medium">{tagKey}</span>
        <span className="text-muted-foreground">: </span>
        {value}
      </span>
      {onRemove ? (
        <button
          type="button"
          aria-label={`Remove ${tagKey}: ${value}`}
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
