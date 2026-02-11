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
  highlight?: boolean;
  options: Option[];
  value: string;
  onChange: (value: string) => void;
};

function SwitchBar({ className, highlight, options, value, onChange }: Props) {
  return (
    <div
      className={cn(
        "flex w-fit gap-1 rounded-sm border border-slate-700 p-2",
        className,
      )}
    >
      {options.map((option) => {
        const selected = option.value === value;
        const optionElement = (
          <div
            key={option.value}
            className={cn(
              "flex cursor-pointer items-center whitespace-nowrap rounded-sm px-3 py-2 text-xs hover:bg-slate-700",
              {
                "bg-slate-700/40": highlight && !selected,
                "bg-slate-700": selected,
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
