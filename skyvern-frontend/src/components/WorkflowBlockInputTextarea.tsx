import { PlusIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import { AutoResizingTextarea } from "./AutoResizingTextarea/AutoResizingTextarea";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";
import { WorkflowBlockParameterSelect } from "@/routes/workflows/editor/nodes/WorkflowBlockParameterSelect";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "./ui/tooltip";
import { useEdges, useNodes } from "@xyflow/react";

type Props = Omit<
  React.ComponentProps<typeof AutoResizingTextarea>,
  "onChange"
> & {
  onChange: (value: string) => void;
  nodeId: string;
  isFirstInputInNode?: boolean;
};

function WorkflowBlockInputTextarea(props: Props) {
  const { nodeId, onChange, ...textAreaProps } = props;
  const edges = useEdges();
  const nodes = useNodes();

  function isInsideFirstNode() {
    const node = nodes.find((node) => node.id === nodeId);
    if (!node) {
      return;
    }
    const incomingEdge = edges.find((edge) => edge.target === node.id);
    if (!incomingEdge) {
      return;
    }
    const source = incomingEdge.source;
    const sourceNode = nodes.find((node) => node.id === source);
    if (!sourceNode) {
      return;
    }
    return !node.parentId && sourceNode.type === "start";
  }

  const showInputTooltip = isInsideFirstNode() && props.isFirstInputInNode;

  return (
    <div className="relative">
      <AutoResizingTextarea
        {...textAreaProps}
        onChange={(event) => {
          onChange(event.target.value);
        }}
        className={cn("pr-9", props.className)}
      />
      <div className="absolute right-0 top-0 flex size-9 cursor-pointer items-center justify-center">
        <Popover>
          <TooltipProvider>
            <Tooltip open={showInputTooltip}>
              <TooltipTrigger asChild>
                <PopoverTrigger asChild>
                  <div className="rounded p-1 hover:bg-muted">
                    <PlusIcon className="size-4" />
                  </div>
                </PopoverTrigger>
              </TooltipTrigger>
              <TooltipContent>Add parameters using the + button</TooltipContent>
            </Tooltip>
          </TooltipProvider>
          <PopoverContent>
            <WorkflowBlockParameterSelect
              nodeId={nodeId}
              onAdd={(parameterKey) => {
                onChange(`${props.value ?? ""}{{${parameterKey}}}`);
              }}
            />
          </PopoverContent>
        </Popover>
      </div>
    </div>
  );
}

export { WorkflowBlockInputTextarea };
