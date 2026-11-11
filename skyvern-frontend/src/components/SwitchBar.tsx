import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";

type Option = {
  label: string;
  value: string;
  helpText?: string;
};

type Props = {
  className?: string;
  options: Option[];
  value: string;
  onChange: (value: string) => void;
};

function SwitchBar({ className, options, value, onChange }: Props) {
  return (
    <div
      className={cn(
        "flex w-fit gap-1 rounded-sm border border-slate-300 p-2 dark:border-slate-700",
        className,
      )}
    >
      {options.map((option) => {
        const selected = option.value === value;
        const optionElement = (
          <div
            key={option.value}
            className={cn(
              "flex cursor-pointer items-center whitespace-nowrap rounded-sm px-3 py-2 text-xs text-slate-700 hover:bg-slate-100 hover:text-slate-950 dark:text-foreground dark:hover:bg-slate-700 dark:hover:text-foreground",
              {
                "bg-neutral-900 !text-white hover:bg-neutral-900 hover:!text-white dark:bg-slate-700 dark:!text-foreground dark:hover:bg-slate-700":
                  selected,
              },
            )}
            onClick={() => {
              if (!selected) {
                onChange(option.value);
              }
            }}
          >
            {option.label}
          </div>
        );

        if (option.helpText) {
          return (
            <TooltipProvider key={option.value}>
              <Tooltip>
                <TooltipTrigger asChild>{optionElement}</TooltipTrigger>
                <TooltipContent>{option.helpText}</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          );
        }

        return optionElement;
      })}
    </div>
  );
}

export { SwitchBar };
