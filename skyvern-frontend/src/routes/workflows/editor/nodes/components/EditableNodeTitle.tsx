import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";
import { HorizontallyResizingInput } from "./HorizontallyResizingInput";
import { useState, useRef, useEffect } from "react";

type Props = {
  value: string;
  editable: boolean;
  onChange: (value: string) => void;
  onValidate?: (value: string) => string | null;
  validationError?: string | null;
  titleClassName?: string;
  inputClassName?: string;
};

function EditableNodeTitle({
  value,
  editable,
  onChange,
  onValidate,
  validationError,
  titleClassName,
  inputClassName,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Clear local error when external validation error is cleared
  useEffect(() => {
    if (!validationError && localError) {
      setLocalError(null);
    }
  }, [validationError, localError]);

  const displayError = localError || validationError;

  const handleBlur = (event: React.FocusEvent<HTMLInputElement>) => {
    if (!editable) {
      event.currentTarget.value = value;
      setEditing(false);
      return;
    }

    const newValue = event.currentTarget.value.trim();

    // If unchanged, just exit edit mode
    if (newValue === value) {
      setEditing(false);
      setLocalError(null);
      return;
    }

    // Validate if validator is provided
    if (onValidate) {
      const error = onValidate(newValue);
      if (error) {
        setLocalError(error);
        // Re-focus the input to let user fix the error
        setTimeout(() => inputRef.current?.focus(), 0);
        return;
      }
    }

    setLocalError(null);
    onChange(newValue);
    setEditing(false);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (!editable) {
      return;
    }
    if (event.key === "Enter") {
      event.currentTarget.blur();
    }
    if (event.key === "Escape") {
      event.currentTarget.value = value;
      setLocalError(null);
      setEditing(false);
    }
  };

  if (!editing) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <h1
              className={cn("cursor-text", titleClassName)}
              onClick={() => {
                if (editable) {
                  setEditing(true);
                  setLocalError(null);
                }
              }}
            >
              {value}
            </h1>
          </TooltipTrigger>
          <TooltipContent>
            {editable ? "Click to rename" : "Block name"}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return (
    <div className="relative">
      <HorizontallyResizingInput
        ref={inputRef}
        disabled={!editable}
        size={1}
        autoFocus
        className={cn(
          "nopan w-min border-0 p-0",
          displayError && "text-destructive",
          inputClassName,
        )}
        onBlur={handleBlur}
        onKeyDown={handleKeyDown}
        onChange={() => {
          // Clear local error while typing
          if (localError) {
            setLocalError(null);
          }
        }}
        defaultValue={value}
        aria-invalid={!!displayError}
        aria-describedby={displayError ? "block-name-error" : undefined}
      />
      {displayError && (
        <div
          id="block-name-error"
          className="absolute left-0 top-full z-10 mt-1 whitespace-nowrap rounded bg-destructive/90 px-2 py-1 text-xs text-destructive-foreground"
        >
          {displayError}
        </div>
      )}
    </div>
  );
}

export { EditableNodeTitle };
