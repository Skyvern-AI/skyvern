import { useState } from "react";
import { Button } from "./button";
import { Input } from "./input";
import { CheckIcon, CopyIcon } from "@radix-ui/react-icons";
import { copyText } from "@/util/copyText";

type Props = {
  value: string;
};

function HiddenCopyableInput({ value }: Props) {
  const [hidden, setHidden] = useState(true);
  const [copied, setCopied] = useState(false);

  const buttonText = hidden ? "Reveal" : copied ? "Copied" : "Copy";
  const inputValue = hidden ? "**** **** **** ****" : value;

  return (
    <div className="relative w-full">
      <Input value={inputValue} className="h-10" readOnly />
      <div className="absolute inset-y-0 right-1 flex items-center">
        <Button
          size="sm"
          variant="secondary"
          className="cursor-pointer"
          onClick={() => {
            if (hidden) {
              setHidden(false);
              return;
            }
            copyText(value).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 3000);
            });
          }}
        >
          {!hidden && !copied && <CopyIcon className="mr-2 h-4 w-4" />}
          {!hidden && copied && <CheckIcon className="mr-2 h-4 w-4" />}
          {buttonText}
        </Button>
      </div>
    </div>
  );
}

export { HiddenCopyableInput };
