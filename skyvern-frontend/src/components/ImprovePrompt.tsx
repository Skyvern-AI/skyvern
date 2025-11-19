import { AxiosError } from "axios";
import { MagicWandIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { SwitchBar } from "@/components/SwitchBar";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { ImprovePromptForWorkflowResponse } from "@/routes/workflows/types/workflowTypes";

interface Props {
  context?: Record<string, unknown>;
  isVisible?: boolean;
  onBegin?: () => void;
  onEnd?: () => void;
  onImprove: (improvedPrompt: string) => void;
  prompt: string;
  size?: "small" | "large";
  useCase: string;
}

function ImprovePrompt(props: Props) {
  const { size = "large" } = props;
  const credentialGetter = useCredentialGetter();
  const [showImproveDialog, setShowImproveDialog] = useState(false);
  const [improvedPrompt, setImprovedPrompt] = useState<string>("");
  const [originalPrompt, setOriginalPrompt] = useState<string>("");
  const [selectedPromptVersion, setSelectedPromptVersion] = useState<
    "improved" | "original"
  >("improved");

  const improvePromptMutation = useMutation({
    mutationFn: async ({ prompt }: { prompt: string }) => {
      props.onBegin?.();
      const client = await getClient(credentialGetter, "sans-api-v1");

      const result = await client.post<
        { prompt: string },
        { data: ImprovePromptForWorkflowResponse }
      >(`/prompts/improve?use-case=${props.useCase}`, {
        context: props.context,
        prompt,
      });

      return result;
    },
    onSuccess: ({ data: { error, improved, original } }) => {
      props.onEnd?.();

      if (error) {
        console.error("Error improving prompt:", error);

        toast({
          variant: "default",
          title:
            "We're sorry - we could not improve upon the prompt at this time.",
          description: `Please try again later.\n\n[${error}]`,
        });

        return;
      }

      setImprovedPrompt(improved);
      setOriginalPrompt(original);
      setSelectedPromptVersion("improved");
      setShowImproveDialog(true);
    },
    onError: (error: AxiosError) => {
      props.onEnd?.();

      toast({
        variant: "destructive",
        title: "Error improving prompt",
        description: error.message,
      });
    },
  });

  return (
    <div
      className={`flex items-center overflow-hidden transition-all duration-300 ${
        props.isVisible
          ? `${size === "large" ? "w-14" : "w-4"} opacity-100`
          : "pointer-events-none w-0 opacity-0"
      }`}
    >
      {improvePromptMutation.isPending ? (
        <ReloadIcon
          className={`size-${size === "large" ? "6" : "4"} shrink-0 animate-spin`}
        />
      ) : (
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <MagicWandIcon
                className={`${size === "large" ? "size-6" : "size-4"} shrink-0 cursor-pointer`}
                onClick={async () => {
                  improvePromptMutation.mutate({
                    prompt: props.prompt,
                  });
                }}
              />
            </TooltipTrigger>
            <TooltipContent>
              <p>Have AI improve your prompt!</p>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      )}
      <Dialog open={showImproveDialog} onOpenChange={setShowImproveDialog}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Choose Your Prompt</DialogTitle>
            <DialogDescription>
              Select which version of the prompt you'd like to use
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <SwitchBar
              options={[
                { label: "Improved", value: "improved" },
                { label: "Original", value: "original" },
              ]}
              value={selectedPromptVersion}
              onChange={(value) =>
                setSelectedPromptVersion(value as "improved" | "original")
              }
            />
            <div className="max-h-96 overflow-y-auto rounded-md border border-slate-700 bg-slate-800 p-4">
              <p className="whitespace-pre-wrap text-sm">
                {selectedPromptVersion === "improved"
                  ? improvedPrompt
                  : originalPrompt}
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="secondary"
              onClick={() => {
                setShowImproveDialog(false);
              }}
            >
              Cancel
            </Button>
            <Button
              onClick={() => {
                props.onImprove(
                  selectedPromptVersion === "improved"
                    ? improvedPrompt
                    : originalPrompt,
                );
                setShowImproveDialog(false);
              }}
            >
              Use This Prompt
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export { ImprovePrompt };
