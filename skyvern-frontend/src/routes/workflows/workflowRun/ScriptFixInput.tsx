import { useEffect, useState } from "react";
import { MagicWandIcon, ReloadIcon } from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/use-toast";
import { useReviewScriptMutation } from "../hooks/useReviewScriptMutation";
import type { ReviewScriptResponse } from "../types/scriptTypes";

interface Props {
  workflowPermanentId: string;
  workflowRunId?: string;
  onScriptUpdated?: (data: ReviewScriptResponse) => void;
}

function ScriptFixInput({
  workflowPermanentId,
  workflowRunId,
  onScriptUpdated,
}: Props) {
  const [isOpen, setIsOpen] = useState(false);
  const [instructions, setInstructions] = useState("");

  // Reset state when navigating between runs or workflows
  useEffect(() => {
    setIsOpen(false);
    setInstructions("");
  }, [workflowRunId, workflowPermanentId]);

  const { mutate, isPending } = useReviewScriptMutation({
    workflowPermanentId,
    onSuccess: (data) => {
      if (data.updated_blocks.length === 0) {
        toast({
          title: "No changes needed",
          description:
            data.message ??
            "The current code already satisfies your instructions.",
        });
      } else {
        toast({
          title: "Script updated",
          description: `Created v${data.version} with ${data.updated_blocks.length} updated block(s).`,
        });
        onScriptUpdated?.(data);
      }
      setIsOpen(false);
      setInstructions("");
    },
  });

  const handleSubmit = () => {
    if (!instructions.trim()) return;
    mutate({
      user_instructions: instructions.trim(),
      workflow_run_id: workflowRunId ?? null,
    });
  };

  return (
    <div className="flex w-full flex-col gap-2">
      {/* Trigger button — always visible in collapsed state */}
      {!isOpen && (
        <div className="flex justify-end">
          <Button
            variant="outline"
            size="sm"
            className="gap-1.5"
            onClick={() => setIsOpen(true)}
          >
            <MagicWandIcon className="h-3.5 w-3.5" />
            Fix with AI
          </Button>
        </div>
      )}
      {/* Expanded input panel */}
      {isOpen && (
        <div className="flex w-full flex-col gap-2 rounded-md border border-slate-700 bg-slate-elevation2 p-3">
          <div className="flex items-center gap-2 text-xs font-medium text-slate-300">
            <MagicWandIcon className="h-3.5 w-3.5" />
            Describe what to fix
          </div>
          <Textarea
            className="min-h-[80px] resize-none bg-slate-elevation3 text-xs"
            placeholder="e.g. The download loop clicks the same file every time. Make each iteration target the specific file using current_value."
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            disabled={isPending}
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                handleSubmit();
              }
            }}
          />
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-slate-500">
              {isPending ? "Reviewing script..." : "Cmd/Ctrl+Enter to submit"}
            </span>
            <div className="flex gap-2">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setIsOpen(false);
                  setInstructions("");
                }}
                disabled={isPending}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={handleSubmit}
                disabled={isPending || !instructions.trim()}
              >
                {isPending && (
                  <ReloadIcon className="mr-1.5 h-3 w-3 animate-spin" />
                )}
                {isPending ? "Reviewing..." : "Fix Script"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export { ScriptFixInput };
