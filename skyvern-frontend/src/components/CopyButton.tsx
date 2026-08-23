import { useState } from "react";
import { CheckIcon, CopyIcon } from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
import { copyText } from "@/util/copyText";

function CopyButton({
  value,
  className,
  ariaLabel,
  id,
  tabIndex,
  onFocus,
}: {
  // A getter defers building the copied text until click, so callers rendering
  // many buttons (e.g. per JSON-tree row) don't serialize on every render.
  value: string | (() => string);
  className?: string;
  ariaLabel?: string;
  // Set together when the button sits in a roving-tabindex toolbar and must be
  // reachable by the toolbar's arrow keys rather than as its own tab stop.
  id?: string;
  tabIndex?: number;
  onFocus?: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (copied) {
      return;
    }
    await copyText(typeof value === "function" ? value() : value);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <Button
      size="icon"
      variant="ghost"
      onClick={handleCopy}
      className={className}
      aria-label={ariaLabel ?? "Copy to clipboard"}
      id={id}
      tabIndex={tabIndex}
      onFocus={onFocus}
    >
      {copied ? <CheckIcon /> : <CopyIcon />}
    </Button>
  );
}

export { CopyButton };
