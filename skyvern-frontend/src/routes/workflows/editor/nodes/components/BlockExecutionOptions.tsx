import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { HelpTooltip } from "@/components/HelpTooltip";
import { helpTooltips } from "../../helpContent";

type FailureMode = "stop" | "continue" | "next_iteration";

function getFailureMode(
  continueOnFailure: boolean,
  nextLoopOnFailure: boolean,
): FailureMode {
  if (continueOnFailure) return "continue";
  if (nextLoopOnFailure) return "next_iteration";
  return "stop";
}

interface BlockExecutionOptionsProps {
  continueOnFailure: boolean;
  nextLoopOnFailure?: boolean;
  includeActionHistoryInVerification?: boolean;
  editable: boolean;
  isInsideForLoop: boolean;
  parentLoopSkipsOnFail?: boolean;
  blockType: string;
  onContinueOnFailureChange: (checked: boolean) => void;
  onNextLoopOnFailureChange: (checked: boolean) => void;
  onIncludeActionHistoryInVerificationChange?: (checked: boolean) => void;
  showOptions?: {
    continueOnFailure?: boolean;
    nextLoopOnFailure?: boolean;
    includeActionHistoryInVerification?: boolean;
  };
  hideTopSeparator?: boolean;
}

export function BlockExecutionOptions({
  continueOnFailure,
  nextLoopOnFailure = false,
  includeActionHistoryInVerification = false,
  editable,
  isInsideForLoop,
  parentLoopSkipsOnFail = false,
  blockType,
  onContinueOnFailureChange,
  onNextLoopOnFailureChange,
  onIncludeActionHistoryInVerificationChange,
  showOptions = {
    continueOnFailure: true,
    nextLoopOnFailure: true,
    includeActionHistoryInVerification: false,
  },
  hideTopSeparator = false,
}: BlockExecutionOptionsProps) {
  const showContinueOnFailure = showOptions.continueOnFailure ?? true;
  const showNextLoopOnFailure = showOptions.nextLoopOnFailure ?? true;
  const showIncludeActionHistory =
    showOptions.includeActionHistoryInVerification ?? false;

  return (
    <>
      {!hideTopSeparator && <Separator />}
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
      {showContinueOnFailure &&
        showNextLoopOnFailure &&
        isInsideForLoop &&
        (() => {
          const childMode = getFailureMode(
            continueOnFailure,
            nextLoopOnFailure,
          );
          // When the parent loop's 'Skip Iterations that Fail' is on, the
          // runtime treats a Stop-mode child as Skip. So we hide the
          // Stop option from the menu and display Skip for those blocks —
          // the dropdown stays editable and the displayed mode matches the
          // runtime.
          const displayMode =
            parentLoopSkipsOnFail && childMode === "stop"
              ? "next_iteration"
              : childMode;
          return (
            <div className="flex items-center justify-between">
              <div className="flex gap-2">
                <Label className="text-xs font-normal text-slate-300">
                  On block failure
                </Label>
                <HelpTooltip
                  content={
                    helpTooltips[blockType as keyof typeof helpTooltips]?.[
                      "onBlockFailure"
                    ] || helpTooltips["task"]["onBlockFailure"]
                  }
                />
              </div>
              <Select
                value={displayMode}
                onValueChange={(value) => {
                  if (!editable) return;
                  const mode = value as FailureMode;
                  onContinueOnFailureChange(mode === "continue");
                  onNextLoopOnFailureChange(mode === "next_iteration");
                }}
                disabled={!editable}
              >
                <SelectTrigger className="nopan w-52 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {!parentLoopSkipsOnFail && (
                    <SelectItem value="stop">Stop the loop</SelectItem>
                  )}
                  <SelectItem value="continue">
                    Continue to next block in this iteration
                  </SelectItem>
                  <SelectItem value="next_iteration">
                    Skip to next iteration
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          );
        })()}
      {showContinueOnFailure &&
        (!isInsideForLoop || !showNextLoopOnFailure) && (
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
      <Separator />
    </>
  );
}
