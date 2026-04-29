import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";
import { HorizontallyResizingInput } from "./HorizontallyResizingInput";
import { useLayoutEffect, useRef, useState } from "react";

type Props = {
  value: string;
  editable: boolean;
  onChange: (value: string) => void;
  titleClassName?: string;
  inputClassName?: string;
};

function EditableNodeTitle({
  value,
  editable,
  onChange,
  titleClassName,
  inputClassName,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [isTruncated, setIsTruncated] = useState(false);
  const titleRef = useRef<HTMLHeadingElement>(null);

  useLayoutEffect(() => {
    if (editing) {
      return;
    }
    const el = titleRef.current;
    if (!el) {
      return;
    }
    const measure = () => {
      setIsTruncated(el.scrollWidth > el.clientWidth);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => {
      observer.disconnect();
    };
  }, [value, editing]);

  if (!editing) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <h1
              ref={titleRef}
              className={cn("min-w-0 cursor-text truncate", titleClassName)}
              onClick={() => {
                setEditing(true);
              }}
            >
              {value}
            </h1>
          </TooltipTrigger>
          <TooltipContent>
            {isTruncated ? value : "Click to edit"}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return (
    <HorizontallyResizingInput
      disabled={!editable}
      size={1}
      autoFocus
      className={cn("nopan w-min border-0 p-0", inputClassName)}
      onBlur={(event) => {
        if (!editable) {
          event.currentTarget.value = value;
          return;
        }
        if (event.currentTarget.value !== value) {
          onChange(event.target.value);
        }
        setEditing(false);
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
      }}
      defaultValue={value}
    />
  );
}

export { EditableNodeTitle };
