import { Textarea } from "@/components/ui/textarea";
import {
  type ChangeEventHandler,
  type HTMLAttributes,
  forwardRef,
  useEffect,
  useRef,
  useCallback,
  useLayoutEffect,
} from "react";
import { cn } from "@/util/utils";

type Props = {
  value: string;
  onChange?: ChangeEventHandler<HTMLTextAreaElement>;
  className?: string;
  readOnly?: boolean;
  disabled?: boolean;
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
      disabled,
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

    const measureAndSize = useCallback(() => {
      const textareaElement = getTextarea();
      if (!textareaElement) return;
      textareaElement.style.height = "auto";
      const measured = textareaElement.scrollHeight;
      // scrollHeight is 0 when the textarea is mounted inside a hidden /
      // mid-animating parent (e.g., a Radix Collapsible just before its
      // open keyframe runs). Leave style.height as "auto" so the textarea
      // stays visible at intrinsic line height; the ResizeObserver below
      // re-measures once the parent settles.
      if (measured === 0) {
        lastHeightRef.current = "";
        return;
      }
      const newHeight = `${measured + 2}px`;
      if (lastHeightRef.current !== newHeight) {
        lastHeightRef.current = newHeight;
        textareaElement.style.height = newHeight;
      } else {
        textareaElement.style.height = lastHeightRef.current;
      }
    }, [getTextarea]);

    useLayoutEffect(() => {
      measureAndSize();
    }, [measureAndSize, value]);

    // Re-measure when the textarea (or its container) actually gains size.
    // Without this, an initial mount inside an animating Collapsible renders
    // at scrollHeight=0 and the measureAndSize fallback above leaves height
    // at "auto"; the observer fires once layout stabilises and locks in
    // the correct height. ResizeObserver is feature-detected so jsdom-based
    // tests that don't polyfill it don't crash.
    useEffect(() => {
      if (typeof ResizeObserver === "undefined") return;
      const textareaElement = getTextarea();
      if (!textareaElement) return;
      const observer = new ResizeObserver(() => {
        if (lastHeightRef.current === "" && textareaElement.scrollHeight > 0) {
          measureAndSize();
        }
      });
      observer.observe(textareaElement);
      return () => observer.disconnect();
    }, [getTextarea, measureAndSize]);

    return (
      <Textarea
        value={value}
        onChange={onChange}
        readOnly={readOnly}
        disabled={disabled}
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
