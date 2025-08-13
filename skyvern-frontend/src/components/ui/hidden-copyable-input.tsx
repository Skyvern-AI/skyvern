import { useState } from "react";
import { Button } from "./button";
import { Input } from "./input";
import { CheckIcon, CopyIcon, EyeOpenIcon, EyeClosedIcon } from "@radix-ui/react-icons";
import { copyText } from "@/util/copyText";
import {
    Tooltip,
    TooltipContent,
    TooltipProvider,
    TooltipTrigger,
} from "@/components/ui/tooltip";

type Props = {
    value: string;
};

function HiddenCopyableInput({ value }: Props) {
    const [hidden, setHidden] = useState(true);
    const [revealTooltipOpen, setRevealTooltipOpen] = useState(false);

    const [copied, setCopied] = useState(false);
    const [copyTooltipOpen, setCopyTooltipOpen] = useState(false);

    const inputValue = hidden ? "**** **** **** ****" : value;

    const handleToggleHidden = () => {
        // Force tooltip content update instantly
        setRevealTooltipOpen(false);
        setHidden((prev) => !prev);
        setTimeout(() => setRevealTooltipOpen(true), 10);
    };

    const handleCopy = () => {
        copyText(value).then(() => {
            setCopied(true);
            setCopyTooltipOpen(true); // force tooltip open

            setTimeout(() => {
                setCopyTooltipOpen(false); // fade out naturally
                setTimeout(() => setCopied(false), 200); // reset state after fade
            }, 3000);
        });
    };

    return (
        <TooltipProvider delayDuration={200}>
            <div className="relative w-full">
                <Input value={inputValue} className="h-10 pr-[7rem]" readOnly />
                <div className="absolute inset-y-0 right-1 flex items-center gap-1">

                    {/* Reveal / Hide button */}
                    <Tooltip open={revealTooltipOpen} onOpenChange={setRevealTooltipOpen}>
                        <TooltipTrigger asChild>
                            <Button
                                size="sm"
                                variant="secondary"
                                onClick={handleToggleHidden}
                                onMouseEnter={() => setRevealTooltipOpen(true)}
                                onMouseLeave={() => setRevealTooltipOpen(false)}
                            >
                                {hidden ? (
                                    <EyeOpenIcon className="h-4 w-4" />
                                ) : (
                                    <EyeClosedIcon className="h-4 w-4" />
                                )}
                            </Button>
                        </TooltipTrigger>
                        <TooltipContent side="top">{hidden ? "Reveal" : "Hide"}</TooltipContent>
                    </Tooltip>

                    {/* Copy button */}
                    <Tooltip open={copyTooltipOpen} onOpenChange={(open) => {
                        if (!copied) setCopyTooltipOpen(open);
                    }}>
                        <TooltipTrigger asChild>
                            <Button
                                size="sm"
                                variant="secondary"
                                onClick={handleCopy}
                            >
                                {copied ? <CheckIcon className="h-4 w-4" /> : <CopyIcon className="h-4 w-4" />}
                            </Button>
                        </TooltipTrigger>
                        <TooltipContent side="top">{copied ? "Copied!" : "Copy"}</TooltipContent>
                    </Tooltip>

                </div>
            </div>
        </TooltipProvider>
    );
}

export { HiddenCopyableInput };
