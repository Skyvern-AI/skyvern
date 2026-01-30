import { Input } from "@/components/ui/input";
import { forwardRef, useLayoutEffect, useRef, useImperativeHandle } from "react";

type Props = React.ComponentPropsWithoutRef<typeof Input>;

const HorizontallyResizingInput = forwardRef<HTMLInputElement, Props>(
  (props, forwardedRef) => {
    const internalRef = useRef<HTMLInputElement>(null);

    // Expose the internal ref to the parent
    useImperativeHandle(forwardedRef, () => internalRef.current!, []);

    useLayoutEffect(() => {
      // size the textarea correctly on first render
      if (!internalRef.current) {
        return;
      }
      internalRef.current.style.width = `${internalRef.current.scrollWidth + 2}px`;
    }, []);

    function setSize() {
      if (!internalRef.current) {
        return;
      }
      internalRef.current.style.width = "auto";
      internalRef.current.style.width = `${internalRef.current.scrollWidth + 2}px`;
    }

    return (
      <Input
        size={1}
        onInput={(event) => {
          setSize();
          props.onInput?.(event);
        }}
        ref={internalRef}
        onKeyDown={(event) => {
          setSize();
          props.onKeyDown?.(event);
        }}
        {...props}
      />
    );
  },
);

HorizontallyResizingInput.displayName = "HorizontallyResizingInput";

export { HorizontallyResizingInput };
