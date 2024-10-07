import { QuestionMarkCircledIcon } from "@radix-ui/react-icons";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "./ui/tooltip";

type Props = {
  content: string;
};

function HelpTooltip({ content }: Props) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <QuestionMarkCircledIcon className="h-4 w-4" />
        </TooltipTrigger>
        <TooltipContent className="max-w-[250px]">{content}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { HelpTooltip };
