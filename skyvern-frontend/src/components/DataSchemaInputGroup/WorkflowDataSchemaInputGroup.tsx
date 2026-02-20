import { HelpTooltip } from "@/components/HelpTooltip";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Cross2Icon,
  MagicWandIcon,
  PaperPlaneIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useMutation } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { helpTooltips } from "@/routes/workflows/editor/helpContent";
import { useCallback, useMemo, useState } from "react";
import { AutoResizingTextarea } from "../AutoResizingTextarea/AutoResizingTextarea";
import { Button } from "../ui/button";
import { AxiosError } from "axios";
import { toast } from "../ui/use-toast";
import { TSON } from "@/util/tson";
import { cn } from "@/util/utils";

type Props = {
  value: string;
  onChange: (value: string) => void;
  suggestionContext: Record<string, unknown>;
  exampleValue: Record<string, unknown>;
  helpTooltip?: string;
};

function WorkflowDataSchemaInputGroup({
  value,
  onChange,
  suggestionContext,
  exampleValue,
  helpTooltip,
}: Props) {
  const credentialGetter = useCredentialGetter();
  const [generateWithAIActive, setGenerateWithAIActive] = useState(false);
  const [generateWithAIPrompt, setGenerateWithAIPrompt] = useState("");
  const [pendingSchema, setPendingSchema] = useState<string | null>(null);

  const resetAIState = useCallback(() => {
    setPendingSchema(null);
    setGenerateWithAIActive(false);
    setGenerateWithAIPrompt("");
  }, []);

  const tsonResult = useMemo(() => {
    if (value === "null") return null;
    return TSON.parse(value);
  }, [value]);

  const getDataSchemaSuggestionMutation = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter);
      return client.post<{ output: Record<string, unknown> }>(
        "/suggest/data_schema",
        {
          input: generateWithAIPrompt,
          context: suggestionContext,
        },
      );
    },
    onSuccess: (response) => {
      if (value === "null") {
        return;
      }
      setPendingSchema(JSON.stringify(response.data.output, null, 2));
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Could not generate the data schema",
        description:
          error.message ?? "There was an error generating data schema",
      });
    },
  });

  return (
    <div className="space-y-2">
      <div className="flex h-7 items-center justify-between">
        <div className="flex gap-4">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">Data Schema</Label>
            <HelpTooltip content={helpTooltip ?? helpTooltips["task"]["dataSchema"]} />
          </div>
          <Checkbox
            checked={value !== "null"}
            onCheckedChange={(checked) => {
              if (!checked) {
                resetAIState();
              }
              onChange(
                checked ? JSON.stringify(exampleValue, null, 2) : "null",
              );
            }}
          />
        </div>
        {value !== "null" && !generateWithAIActive && (
          <Button
            variant="tertiary"
            className="h-7 text-xs"
            onClick={() => {
              setGenerateWithAIActive(true);
            }}
          >
            <MagicWandIcon className="mr-2 size-4" />
            Generate with AI
          </Button>
        )}
      </div>

      {value !== "null" && (
        <div className="space-y-2">
          {generateWithAIActive ? (
            <div className="flex w-full items-center rounded-xl border px-4">
              <Cross2Icon
                className="size-4 cursor-pointer"
                onClick={() => {
                  getDataSchemaSuggestionMutation.reset();
                  resetAIState();
                }}
              />
              <AutoResizingTextarea
                className="min-h-0 resize-none rounded-md border-transparent px-4 py-2 text-xs hover:border-transparent focus-visible:ring-0"
                value={generateWithAIPrompt}
                onChange={(event) => {
                  setGenerateWithAIPrompt(event.target.value);
                }}
                placeholder="Describe how you want your output formatted"
              />
              {getDataSchemaSuggestionMutation.isPending ? (
                <ReloadIcon className="size-4 animate-spin" />
              ) : (
                <PaperPlaneIcon
                  className={cn(
                    "size-4",
                    pendingSchema !== null || !generateWithAIPrompt.trim()
                      ? "cursor-not-allowed opacity-50"
                      : "cursor-pointer",
                  )}
                  onClick={() => {
                    if (
                      pendingSchema !== null ||
                      !generateWithAIPrompt.trim()
                    ) {
                      return;
                    }
                    getDataSchemaSuggestionMutation.mutate();
                  }}
                />
              )}
            </div>
          ) : null}
          <div
            className={cn(
              "rounded-md",
              tsonResult && !tsonResult.success
                ? "ring-1 ring-red-500"
                : undefined,
            )}
          >
            <CodeEditor
              language="json"
              value={value}
              onChange={onChange}
              className="nopan"
              fontSize={8}
            />
          </div>
          {tsonResult !== null && !tsonResult.success && tsonResult.error && (
            <div className="text-xs text-red-400">{tsonResult.error}</div>
          )}
        </div>
      )}
      {value !== "null" && (
        <Dialog
          open={pendingSchema !== null}
          onOpenChange={(open) => {
            if (!open) {
              getDataSchemaSuggestionMutation.reset();
              resetAIState();
            }
          }}
        >
          <DialogContent className="max-w-4xl">
            <DialogHeader>
              <DialogTitle>Review AI-Generated Schema</DialogTitle>
              <DialogDescription>
                Review the AI-generated schema before applying it. This will
                replace your current data schema.
              </DialogDescription>
            </DialogHeader>
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label className="text-sm text-slate-400">Current Schema</Label>
                <CodeEditor
                  language="json"
                  value={value}
                  readOnly
                  className="nopan"
                  fontSize={10}
                  minHeight="200px"
                  maxHeight="400px"
                />
              </div>
              <div className="space-y-2">
                <Label className="text-sm text-slate-400">
                  Proposed Schema
                </Label>
                <CodeEditor
                  language="json"
                  value={pendingSchema ?? ""}
                  readOnly
                  className="nopan"
                  fontSize={10}
                  minHeight="200px"
                  maxHeight="400px"
                />
              </div>
            </div>
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="secondary">Cancel</Button>
              </DialogClose>
              <Button
                onClick={() => {
                  if (pendingSchema !== null) {
                    onChange(pendingSchema);
                  }
                  resetAIState();
                }}
              >
                Accept Changes
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}

export { WorkflowDataSchemaInputGroup };
