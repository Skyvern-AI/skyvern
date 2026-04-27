import { PlusIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import { AutoResizingTextarea } from "./AutoResizingTextarea/AutoResizingTextarea";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";
import { WorkflowBlockParameterSelect } from "@/routes/workflows/editor/nodes/WorkflowBlockParameterSelect";
import { useEffect, useRef, useState } from "react";
import { useDebouncedCallback } from "use-debounce";
import { useParameterAutocomplete } from "@/hooks/useParameterAutocomplete";
import { ParameterAutocompleteDropdown } from "./ParameterAutocompleteDropdown";
import { ParameterGhostText } from "./ParameterGhostText";

import { ImprovePrompt } from "./ImprovePrompt";

interface AiImprove {
  context?: Record<string, unknown>;
  useCase: string;
}

type Props = Omit<
  React.ComponentProps<typeof AutoResizingTextarea>,
  "onChange"
> & {
  aiImprove?: AiImprove;
  extraAction?: React.ReactNode;
  hideActions?: boolean;
  onChange: (value: string) => void;
  nodeId: string;
};

function WorkflowBlockInputTextarea(props: Props) {
  const {
    aiImprove,
    extraAction,
    hideActions,
    nodeId,
    onChange,
    disabled,
    ...textAreaProps
  } = props;
  const showActions = !disabled && !hideActions;
  const [internalValue, setInternalValue] = useState(props.value ?? "");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [cursorPosition, setCursorPosition] = useState<{
    start: number;
    end: number;
  } | null>(null);

  useEffect(() => {
    setInternalValue(props.value ?? "");
  }, [props.value]);

  const doOnChange = useDebouncedCallback((value: string) => {
    onChange(value);
  }, 300);

  const handleTextareaSelect = () => {
    if (textareaRef.current) {
      setCursorPosition({
        start: textareaRef.current.selectionStart,
        end: textareaRef.current.selectionEnd,
      });
    }
  };

  const insertParameterAtCursor = (parameterKey: string) => {
    const value = props.value ?? "";
    const parameterText = `{{${parameterKey}}}`;

    if (cursorPosition && textareaRef.current) {
      const { start, end } = cursorPosition;
      const newValue =
        value.substring(0, start) + parameterText + value.substring(end);

      doOnChange(newValue);

      setTimeout(() => {
        if (textareaRef.current) {
          const newPosition = start + parameterText.length;
          textareaRef.current.focus();
          textareaRef.current.setSelectionRange(newPosition, newPosition);
        }
      }, 0);
    } else {
      doOnChange(`${value}${parameterText}`);
    }
  };

  const handleOnChange = (value: string) => {
    setInternalValue(value);
    handleTextareaSelect();
    doOnChange(value);
  };

  const autocomplete = useParameterAutocomplete({
    nodeId,
    value: String(internalValue),
    inputRef: textareaRef,
    variant: "textarea",
  });

  const handleAutocompleteSelect = (key: string) => {
    const { newValue, cursorPos } = autocomplete.buildSelectedValue(key);
    setInternalValue(newValue);
    doOnChange(newValue);
    autocomplete.dismiss();
    setTimeout(() => {
      if (textareaRef.current) {
        textareaRef.current.focus();
        textareaRef.current.setSelectionRange(cursorPos, cursorPos);
      }
    }, 0);
  };

  const ACTION_ZONE_PADDING = { 1: "pr-9", 2: "pr-12", 3: "pr-16" } as const;
  const actionSlots = 1 + (aiImprove ? 1 : 0) + (extraAction ? 1 : 0);
  // Fall back to the widest padding if a future caller adds a 4th action;
  // dropping right-padding entirely would let icons overlap input text.
  const actionZonePadding =
    ACTION_ZONE_PADDING[actionSlots as keyof typeof ACTION_ZONE_PADDING] ??
    "pr-16";

  return (
    <div className="relative">
      <AutoResizingTextarea
        {...textAreaProps}
        disabled={disabled}
        value={internalValue}
        ref={textareaRef}
        onBlur={() => {
          doOnChange.flush();
        }}
        onChange={(event) => {
          handleOnChange(event.target.value);
        }}
        onClick={handleTextareaSelect}
        onKeyUp={handleTextareaSelect}
        onKeyDown={(e) => {
          if (autocomplete.isOpen) {
            const handled = autocomplete.handleKeyDown(e);
            if (handled && (e.key === "Enter" || e.key === "Tab")) {
              const param = autocomplete.getSelectedParameter();
              if (param) {
                handleAutocompleteSelect(param.key);
              }
            }
          }
        }}
        onSelect={handleTextareaSelect}
        className={cn(showActions && actionZonePadding, props.className)}
      />
      <ParameterGhostText
        ghostText={autocomplete.ghostText}
        textBeforeCursor={autocomplete.textBeforeCursor}
        inputRef={textareaRef}
        variant="textarea"
      />
      <ParameterAutocompleteDropdown
        items={autocomplete.filteredItems}
        selectedIndex={autocomplete.selectedIndex}
        anchorPosition={autocomplete.anchorPosition}
        visible={autocomplete.isOpen}
        onSelect={handleAutocompleteSelect}
        onDismiss={autocomplete.dismiss}
      />

      {showActions && (
        <div className="absolute right-1 top-0 flex size-9 items-center justify-end">
          <div className="flex items-center justify-center gap-1">
            {aiImprove && (
              <ImprovePrompt
                context={aiImprove.context}
                isVisible={Boolean(internalValue.trim())}
                size="small"
                prompt={internalValue}
                onImprove={(prompt) => handleOnChange(prompt)}
                useCase={aiImprove.useCase}
              />
            )}
            {extraAction}
            <div className="cursor-pointer">
              <Popover>
                <PopoverTrigger asChild>
                  <div className="rounded p-1 hover:bg-muted">
                    <PlusIcon className="size-4" />
                  </div>
                </PopoverTrigger>
                <PopoverContent className="w-fit max-w-sm">
                  <WorkflowBlockParameterSelect
                    nodeId={nodeId}
                    onAdd={insertParameterAtCursor}
                  />
                </PopoverContent>
              </Popover>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export { WorkflowBlockInputTextarea };
