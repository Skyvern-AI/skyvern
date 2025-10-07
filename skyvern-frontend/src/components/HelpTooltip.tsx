import { QuestionMarkCircledIcon } from "@radix-ui/react-icons";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "./ui/tooltip";

type Props = {
  className?: string;
  content: string;
};

function HelpTooltip({ content, className }: Props) {
  return (
    <TooltipProvider>
      <Tooltip delayDuration={300}>
        <TooltipTrigger className={className} asChild>
          <QuestionMarkCircledIcon className="size-4" />
        </TooltipTrigger>
        <TooltipContent className="max-w-[250px]">{content}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { HelpTooltip };
