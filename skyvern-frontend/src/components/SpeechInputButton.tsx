import { MicIcon } from "@/components/icons/MicIcon";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";

type SpeechInputButtonProps = {
  isSupported: boolean;
  isListening: boolean;
  isHearingSpeech: boolean;
  disabled?: boolean;
  onToggle: () => void;
  className?: string;
  iconClassName?: string;
};

function SpeechInputButton({
  isSupported,
  isListening,
  isHearingSpeech,
  disabled = false,
  onToggle,
  className,
  iconClassName = "h-4 w-4",
}: SpeechInputButtonProps) {
  const isDisabled = disabled || !isSupported;
  const tooltipLabel = !isSupported
    ? "Voice input isn't supported in this browser. Try Chrome or Edge."
    : isListening
      ? "Stop dictating"
      : "Dictate message";

  const button = (
    <button
      type="button"
      onClick={onToggle}
      disabled={isDisabled}
      aria-label={isListening ? "Stop dictating" : "Dictate message"}
      aria-pressed={isListening}
      className={cn(
        "flex shrink-0 items-center justify-center rounded-md border",
        isListening
          ? "border-destructive bg-destructive/10 text-destructive"
          : "border-input bg-slate-elevation2 text-muted-foreground hover:bg-accent hover:text-accent-foreground",
        isListening &&
          isHearingSpeech &&
          "animate-pulse ring-2 ring-destructive/50",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
    >
      <MicIcon className={iconClassName} />
    </button>
  );

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          {isDisabled ? <span className="inline-flex">{button}</span> : button}
        </TooltipTrigger>
        <TooltipContent>{tooltipLabel}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { SpeechInputButton };
