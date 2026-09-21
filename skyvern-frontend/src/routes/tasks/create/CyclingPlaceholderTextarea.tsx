import { type ComponentProps, forwardRef } from "react";

import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";

import { useCyclingPlaceholder } from "./useCyclingPlaceholder";

type Props = Omit<
  ComponentProps<typeof AutoResizingTextarea>,
  "placeholder"
> & {
  cycling: boolean;
};

// Leaf so the placeholder's typing tick re-renders only this textarea.
const CyclingPlaceholderTextarea = forwardRef<HTMLTextAreaElement, Props>(
  function CyclingPlaceholderTextarea({ cycling, ...props }, ref) {
    const placeholder = useCyclingPlaceholder(cycling);
    return (
      <AutoResizingTextarea ref={ref} placeholder={placeholder} {...props} />
    );
  },
);

export { CyclingPlaceholderTextarea };
