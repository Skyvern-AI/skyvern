import { Textarea } from "@/components/ui/textarea";
import type { ChangeEventHandler, HTMLAttributes } from "react";
import { forwardRef, useEffect, useRef, useCallback } from "react";
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

    useEffect(() => {
      const textareaElement = getTextarea();
      if (!textareaElement) {
        return;
      }
      textareaElement.style.height = "auto";
      textareaElement.style.height = `${textareaElement.scrollHeight + 2}px`;
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
