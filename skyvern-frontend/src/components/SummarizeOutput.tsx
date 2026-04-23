import { MagicWandIcon, ReloadIcon, SymbolIcon } from "@radix-ui/react-icons";
import { useMutation } from "@tanstack/react-query";
import { AxiosError } from "axios";

import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

interface SummarizeOutputResponse {
  error: string | null;
  summary: string;
}

const MAX_OUTPUT_JSON_LENGTH = 100_000;

interface SummarizeOutputProps {
  /**
   * Stable key identifying the full request context (e.g. workflow run id +
   * block id). If this changes between mutate() and response, the response is
   * dropped. Parents MUST pass a key that changes when block/run changes so
   * two contexts that happen to stringify to the same outputJson don't
   * collide.
   */
  contextKey: string;
  outputJson: string;
  workflowTitle?: string | null;
  blockLabel?: string | null;
  hasSummary: boolean;
  onSummary: (summary: string) => void;
}

function SummarizeOutput(props: SummarizeOutputProps) {
  const credentialGetter = useCredentialGetter();

  const mutation = useMutation({
    mutationFn: async () => {
      const requestedJson = props.outputJson;
      const requestedContextKey = props.contextKey;
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.post<
        unknown,
        { data: SummarizeOutputResponse }
      >("/prompts/summarize-output", {
        output_json: requestedJson,
        workflow_title: props.workflowTitle,
        block_label: props.blockLabel,
      });
      return { response, requestedJson, requestedContextKey };
    },
    onSuccess: ({ response, requestedJson, requestedContextKey }) => {
      if (
        requestedContextKey !== props.contextKey ||
        requestedJson !== props.outputJson
      ) {
        return;
      }
      const { data } = response;
      if (data.error !== null) {
        toast({
          variant: "default",
          title: "Could not summarize the output at this time.",
          description: data.error,
        });
        return;
      }
      props.onSummary(data.summary);
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        variant: "destructive",
        title: "Error summarizing output",
        description: detail ?? error.message,
      });
    },
  });

  const handleClick = () => {
    if (props.outputJson.length > MAX_OUTPUT_JSON_LENGTH) {
      toast({
        variant: "default",
        title: "Output too large to summarize.",
        description: `Output exceeds the ${MAX_OUTPUT_JSON_LENGTH.toLocaleString()} character limit.`,
      });
      return;
    }
    mutation.mutate();
  };

  const isRetry = props.hasSummary && !mutation.isPending;
  const label = mutation.isPending
    ? "Summarizing"
    : isRetry
      ? "Retry summary"
      : "Summarize with AI";
  const tooltip = isRetry ? "Retry" : "Summarize with AI";

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            disabled={mutation.isPending}
            onClick={handleClick}
            aria-label={label}
          >
            {mutation.isPending ? (
              <ReloadIcon className="size-4 animate-spin" />
            ) : isRetry ? (
              <SymbolIcon className="size-4" />
            ) : (
              <MagicWandIcon className="size-4" />
            )}
          </Button>
        </TooltipTrigger>
        <TooltipContent>
          <p>{tooltip}</p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { SummarizeOutput };
