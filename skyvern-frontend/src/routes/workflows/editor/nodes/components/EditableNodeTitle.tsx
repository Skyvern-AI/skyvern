import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";
import { HorizontallyResizingInput } from "./HorizontallyResizingInput";
import { type ReactNode, useRef, useState } from "react";

type Props = {
  value: string;
  editable: boolean;
  onChange: (value: string) => void;
  titleClassName?: string;
  inputClassName?: string;
  // Replaces the default click-to-edit <h1> in idle mode; the caller renders
  // idle (e.g. a link) and enters edit via startEditing. Edit flow unchanged.
  renderIdle?: (args: { startEditing: () => void }) => ReactNode;
};

function EditableNodeTitle({
  value,
  editable,
  onChange,
  titleClassName,
  inputClassName,
  renderIdle,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [isTruncated, setIsTruncated] = useState(false);
  const titleRef = useRef<HTMLHeadingElement>(null);

  // Measure on hover rather than via a persistent ResizeObserver: an
  // observer-driven setState here feeds a canvas relayout loop (React #185).
  const measureTruncation = () => {
    const el = titleRef.current;
    if (el) {
      setIsTruncated(el.scrollWidth > el.clientWidth);
    }
  };

  if (!editing) {
    if (renderIdle) {
      return <>{renderIdle({ startEditing: () => setEditing(true) })}</>;
    }
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <h1
              ref={titleRef}
              className={cn("min-w-0 cursor-text truncate", titleClassName)}
              onPointerEnter={measureTruncation}
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
      // HorizontallyResizingInput sets an inline pixel width with no
      // ceiling; max-w-full caps it at the row's allotted space.
      className={cn("nopan w-min max-w-full border-0 p-0", inputClassName)}
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
