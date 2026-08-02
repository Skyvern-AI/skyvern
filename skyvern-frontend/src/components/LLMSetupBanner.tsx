import { Link } from "react-router-dom";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useLLMDiagnostics } from "@/hooks/useLLMDiagnostics";

function LLMSetupBanner() {
  const { data, error, isLoading } = useLLMDiagnostics();

  if (isLoading || error || !data || data.status === "ok") {
    return null;
  }

  const primaryIssue = data.issues[0];
  const missingEnvVars = primaryIssue?.missing_env_vars ?? [];
  const defaultIssueReason =
    missingEnvVars.length > 0
      ? ` because ${missingEnvVars.join(", ")} ${
          missingEnvVars.length === 1 ? "is" : "are"
        } missing.`
      : ".";

  return (
    <div className="px-4 pt-4">
      <Alert className="flex flex-col items-center gap-2 border-amber-700 bg-amber-950 text-amber-50">
        <AlertTitle className="text-center text-base font-semibold tracking-wide">
          LLM setup required
        </AlertTitle>
        <AlertDescription className="space-y-3 text-center text-sm leading-6">
          <p>
            {data.detail ??
              "Skyvern is running, but no usable LLM is configured for local OSS."}{" "}
            Add an Ollama, OpenRouter, or OpenAI-compatible model in Settings.
          </p>
          {primaryIssue ? (
            <p className="text-xs text-amber-100">
              Default LLM <code>{data.default_llm_key}</code> is not ready
              {defaultIssueReason}
            </p>
          ) : null}
          {data.next_step ? (
            <p className="text-xs text-amber-100">{data.next_step}</p>
          ) : null}
          <div className="flex justify-center">
            <Button asChild variant="secondary">
              <Link to="/settings#custom-llms">Configure Custom LLMs</Link>
            </Button>
          </div>
        </AlertDescription>
      </Alert>
    </div>
  );
}

export { LLMSetupBanner };
