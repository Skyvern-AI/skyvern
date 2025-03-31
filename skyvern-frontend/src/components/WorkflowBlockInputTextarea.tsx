import { PlusIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import { AutoResizingTextarea } from "./AutoResizingTextarea/AutoResizingTextarea";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";
import { WorkflowBlockParameterSelect } from "@/routes/workflows/editor/nodes/WorkflowBlockParameterSelect";

type Props = Omit<
  React.ComponentProps<typeof AutoResizingTextarea>,
  "onChange"
> & {
  onChange: (value: string) => void;
  nodeId: string;
};

function WorkflowBlockInputTextarea(props: Props) {
  const { nodeId, onChange, ...textAreaProps } = props;

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
          <PopoverTrigger asChild>
            <div className="rounded p-1 hover:bg-muted">
              <PlusIcon className="size-4" />
            </div>
          </PopoverTrigger>
          <PopoverContent className="w-[22rem]">
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
