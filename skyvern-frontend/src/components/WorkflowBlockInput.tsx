import { PlusIcon, EyeOpenIcon, EyeClosedIcon } from "@radix-ui/react-icons";
import { useState } from "react";
import { cn } from "@/util/utils";
import { Input } from "./ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";
import { WorkflowBlockParameterSelect } from "@/routes/workflows/editor/nodes/WorkflowBlockParameterSelect";

type Props = Omit<React.ComponentProps<typeof Input>, "onChange"> & {
  onChange: (value: string) => void;
  nodeId: string;
};

function WorkflowBlockInput(props: Props) {
  const { nodeId, onChange, type, ...inputProps } = props;
  const [showPassword, setShowPassword] = useState(false);

  const isPasswordField = type === "password";
  const actualType = isPasswordField && showPassword ? "text" : type;

  return (
    <div className="relative">
      <Input
        {...inputProps}
        type={actualType}
        className={cn(isPasswordField ? "pr-18" : "pr-9", props.className)}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
      <div className="absolute right-0 top-0 flex cursor-pointer items-center justify-center">
        {isPasswordField && (
          <div className="flex size-9 items-center justify-center">
            <div
              className="rounded p-1 hover:bg-muted"
              onClick={() => setShowPassword(!showPassword)}
            >
              {showPassword ? (
                <EyeClosedIcon className="size-4" />
              ) : (
                <EyeOpenIcon className="size-4" />
              )}
            </div>
          </div>
        )}
        <div className="flex size-9 items-center justify-center">
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
    </div>
  );
}

export { WorkflowBlockInput };
