import { Input } from "@/components/ui/input";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";
import { useLayoutEffect, useRef } from "react";

type Props = {
  value: string;
  editable: boolean;
  onChange: (value: string) => void;
  className?: string;
};

function EditableNodeTitle({ value, editable, onChange, className }: Props) {
  const ref = useRef<HTMLInputElement>(null);

  useLayoutEffect(() => {
    // size the textarea correctly on first render
    if (!ref.current) {
      return;
    }
    ref.current.style.width = `${ref.current.scrollWidth + 2}px`;
  }, []);

  function setSize() {
    if (!ref.current) {
      return;
    }
    ref.current.style.width = "auto";
    ref.current.style.width = `${ref.current.scrollWidth + 2}px`;
  }

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Input
            disabled={!editable}
            ref={ref}
            size={1}
            className={cn("nopan w-min border-0 p-0", className)}
            onBlur={(event) => {
              if (!editable) {
                event.currentTarget.value = value;
                return;
              }
              onChange(event.target.value);
            }}
            onKeyDown={(event) => {
              if (!editable) {
                return;
              }
              if (event.key === "Enter") {
                event.currentTarget.blur();
              }
              if (event.key === "Escape") {
                event.currentTarget.value = value;
                event.currentTarget.blur();
              }
              setSize();
            }}
            onInput={setSize}
            defaultValue={value}
          />
        </TooltipTrigger>
        <TooltipContent>Click to edit</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { EditableNodeTitle };
