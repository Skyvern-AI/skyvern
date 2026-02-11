import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

function Tip({
  asChild = true,
  children,
  content,
}: {
  asChild?: boolean;
  children: React.ReactNode;
  content: string | null;
}) {
  if (content === null) {
    return children;
  }

  return (
    <TooltipProvider>
      <Tooltip delayDuration={300}>
        <TooltipTrigger asChild={asChild}>{children}</TooltipTrigger>
        <TooltipContent className="max-w-[250px]">{content}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { Tip };
