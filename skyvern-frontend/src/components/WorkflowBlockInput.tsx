import { PlusIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import { Input } from "./ui/input";

type Props = React.ComponentProps<typeof Input> & {
  onIconClick: () => void;
};

function WorkflowBlockInput(props: Props) {
  return (
    <div className="relative">
      <Input {...props} className={cn("pr-9", props.className)} />
      <div className="absolute right-0 top-0 flex size-9 cursor-pointer items-center justify-center">
        <div className="rounded p-1 hover:bg-muted" onClick={props.onIconClick}>
          <PlusIcon className="size-4" />
        </div>
      </div>
    </div>
  );
}

export { WorkflowBlockInput };
