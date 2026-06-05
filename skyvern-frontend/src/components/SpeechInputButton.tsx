import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";

function MicIcon({ className }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 15 15"
      fill="currentColor"
      className={className}
      aria-hidden="true"
    >
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M7.5 1.25a2.25 2.25 0 0 0-2.25 2.25v3.5a2.25 2.25 0 0 0 4.5 0V3.5A2.25 2.25 0 0 0 7.5 1.25Zm-3.75 5.25a3.75 3.75 0 0 0 7.5 0H10a4.75 4.75 0 0 1-9.5 0H3.75ZM7.5 10.25a.75.75 0 0 1 .75.75v1.5h1.25a.75.75 0 0 1 0 1.5H5.5a.75.75 0 0 1 0-1.5h1.25v-1.5A.75.75 0 0 1 7.5 10.25Z"
      />
    </svg>
  );
}

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
