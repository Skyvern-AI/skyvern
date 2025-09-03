import { useState } from "react";
import { CheckIcon, CopyIcon } from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    if (copied) {
      return;
    }
    window.navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <Button size="icon" variant="ghost" onClick={handleCopy}>
      {copied ? <CheckIcon /> : <CopyIcon />}
    </Button>
  );
}

export { CopyButton };
