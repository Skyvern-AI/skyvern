import { Textarea } from "@/components/ui/textarea";
import type { ChangeEventHandler, HTMLAttributes } from "react";
import { forwardRef, useRef, useCallback, useLayoutEffect } from "react";
import { cn } from "@/util/utils";

type Props = {
  value: string;
  onChange?: ChangeEventHandler<HTMLTextAreaElement>;
  className?: string;
  readOnly?: boolean;
  placeholder?: string;
  onClick?: React.MouseEventHandler<HTMLTextAreaElement>;
  onKeyUp?: React.KeyboardEventHandler<HTMLTextAreaElement>;
  onSelect?: React.ReactEventHandler<HTMLTextAreaElement>;
} & Omit<HTMLAttributes<HTMLTextAreaElement>, "onChange" | "value">;

const AutoResizingTextarea = forwardRef<HTMLTextAreaElement, Props>(
  (
    {
      value,
      onChange,
      className,
      readOnly,
      placeholder,
      onClick,
      onKeyUp,
      onSelect,
      ...restProps
    },
    forwardedRef,
  ) => {
    const innerRef = useRef<HTMLTextAreaElement | null>(null);
    const lastHeightRef = useRef<string>("");
    const getTextarea = useCallback(() => innerRef.current, []);

    const setRefs = (element: HTMLTextAreaElement | null) => {
      innerRef.current = element;

      // Forward to external ref
      if (typeof forwardedRef === "function") {
        forwardedRef(element);
      } else if (forwardedRef) {
        forwardedRef.current = element;
      }
    };

    useLayoutEffect(() => {
      const textareaElement = getTextarea();
      if (!textareaElement) {
        return;
      }

      // Temporarily set to auto to measure scrollHeight accurately
      textareaElement.style.height = "auto";
      const newHeight = `${textareaElement.scrollHeight + 2}px`;

      // Only apply the final height if it differs from the last applied height
      // This prevents unnecessary dimension change events in React Flow
      if (lastHeightRef.current !== newHeight) {
        lastHeightRef.current = newHeight;
        textareaElement.style.height = newHeight;
      } else {
        // Restore the previous height since nothing changed
        textareaElement.style.height = lastHeightRef.current;
      }
    }, [getTextarea, value]);

    return (
      <Textarea
        value={value}
        onChange={onChange}
        readOnly={readOnly}
        placeholder={placeholder}
        onClick={onClick}
        onKeyUp={onKeyUp}
        onSelect={onSelect}
        ref={setRefs}
        rows={1}
        className={cn("min-h-0 resize-none overflow-y-hidden", className)}
        {...restProps}
      />
    );
  },
);

export { AutoResizingTextarea };
