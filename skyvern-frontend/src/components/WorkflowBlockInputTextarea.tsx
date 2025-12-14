import { PlusIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import { AutoResizingTextarea } from "./AutoResizingTextarea/AutoResizingTextarea";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";
import { WorkflowBlockParameterSelect } from "@/routes/workflows/editor/nodes/WorkflowBlockParameterSelect";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { useEffect, useRef, useState } from "react";
import { useDebouncedCallback } from "use-debounce";

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
  canWriteTitle?: boolean;
  onChange: (value: string) => void;
  nodeId: string;
};

function WorkflowBlockInputTextarea(props: Props) {
  const { maybeAcceptTitle, maybeWriteTitle } = useWorkflowTitleStore();
  const {
    aiImprove,
    nodeId,
    onChange,
    canWriteTitle = false,
    ...textAreaProps
  } = props;
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

    if (canWriteTitle) {
      maybeWriteTitle(value);
      maybeAcceptTitle();
    }
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

  return (
    <div className="relative">
      <AutoResizingTextarea
        {...textAreaProps}
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
        onSelect={handleTextareaSelect}
        className={cn(`${aiImprove ? "pr-12" : "pr-9"}`, props.className)}
      />

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
          <div className="cursor-pointer">
            <Popover>
              <PopoverTrigger asChild>
                <div className="rounded p-1 hover:bg-muted">
                  <PlusIcon className="size-4" />
                </div>
              </PopoverTrigger>
              <PopoverContent className="w-[22rem]">
                <WorkflowBlockParameterSelect
                  nodeId={nodeId}
                  onAdd={insertParameterAtCursor}
                />
              </PopoverContent>
            </Popover>
          </div>
        </div>
      </div>
    </div>
  );
}

export { WorkflowBlockInputTextarea };
