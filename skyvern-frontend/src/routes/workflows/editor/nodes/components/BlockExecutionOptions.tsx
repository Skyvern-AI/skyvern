import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { HelpTooltip } from "@/components/HelpTooltip";
import { helpTooltips } from "../../helpContent";

interface BlockExecutionOptionsProps {
  continueOnFailure: boolean;
  nextLoopOnFailure?: boolean;
  includeActionHistoryInVerification?: boolean;
  editable: boolean;
  isInsideForLoop: boolean;
  blockType: string;
  onContinueOnFailureChange: (checked: boolean) => void;
  onNextLoopOnFailureChange: (checked: boolean) => void;
  onIncludeActionHistoryInVerificationChange?: (checked: boolean) => void;
  showOptions?: {
    continueOnFailure?: boolean;
    nextLoopOnFailure?: boolean;
    includeActionHistoryInVerification?: boolean;
  };
}

export function BlockExecutionOptions({
  continueOnFailure,
  nextLoopOnFailure = false,
  includeActionHistoryInVerification = false,
  editable,
  isInsideForLoop,
  blockType,
  onContinueOnFailureChange,
  onNextLoopOnFailureChange,
  onIncludeActionHistoryInVerificationChange,
  showOptions = {
    continueOnFailure: true,
    nextLoopOnFailure: true,
    includeActionHistoryInVerification: false,
  },
}: BlockExecutionOptionsProps) {
  const showContinueOnFailure = showOptions.continueOnFailure ?? true;
  const showNextLoopOnFailure = showOptions.nextLoopOnFailure ?? true;
  const showIncludeActionHistory =
    showOptions.includeActionHistoryInVerification ?? false;

  return (
    <>
      <Separator />
      {showIncludeActionHistory &&
        onIncludeActionHistoryInVerificationChange && (
          <div className="flex items-center justify-between">
            <div className="flex gap-2">
              <Label className="text-xs font-normal text-slate-300">
                Include Action History
              </Label>
              <HelpTooltip
                content={
                  helpTooltips[blockType as keyof typeof helpTooltips]?.[
                    "includeActionHistoryInVerification"
                  ] ||
                  helpTooltips["task"]["includeActionHistoryInVerification"]
                }
              />
            </div>
            <div className="w-52">
              <Switch
                checked={includeActionHistoryInVerification}
                onCheckedChange={(checked) => {
                  if (!editable) {
                    return;
                  }
                  onIncludeActionHistoryInVerificationChange(checked);
                }}
              />
            </div>
          </div>
        )}
      {showContinueOnFailure && (
        <div className="flex items-center justify-between">
          <div className="flex gap-2">
            <Label className="text-xs font-normal text-slate-300">
              Continue on Failure
            </Label>
            <HelpTooltip
              content={
                helpTooltips[blockType as keyof typeof helpTooltips]?.[
                  "continueOnFailure"
                ] || helpTooltips["task"]["continueOnFailure"]
              }
            />
          </div>
          <div className="w-52">
            <Switch
              checked={continueOnFailure}
              onCheckedChange={(checked) => {
                if (!editable) {
                  return;
                }
                onContinueOnFailureChange(checked);
              }}
            />
          </div>
        </div>
      )}
      {showNextLoopOnFailure && isInsideForLoop && (
        <div className="flex items-center justify-between">
          <div className="flex gap-2">
            <Label className="text-xs font-normal text-slate-300">
              Next Loop on Failure
            </Label>
            <HelpTooltip
              content={
                helpTooltips[blockType as keyof typeof helpTooltips]?.[
                  "nextLoopOnFailure"
                ] || helpTooltips["task"]["nextLoopOnFailure"]
              }
            />
          </div>
          <div className="w-52">
            <Switch
              checked={nextLoopOnFailure}
              onCheckedChange={(checked) => {
                if (!editable) {
                  return;
                }
                onNextLoopOnFailureChange(checked);
              }}
            />
          </div>
        </div>
      )}
      <Separator />
    </>
  );
}
