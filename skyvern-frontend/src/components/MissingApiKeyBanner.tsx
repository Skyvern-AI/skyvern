import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { apiBaseUrl, envCredential } from "@/util/env";

function MissingApiKeyBanner() {
  const [isRepairing, setIsRepairing] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const isDevMode = import.meta.env.MODE === "development";
  const key = envCredential?.trim() ?? "";
  const shouldShowBanner =
    isDevMode && (key.length === 0 || key === "YOUR_API_KEY");
  if (!shouldShowBanner) {
    return null;
  }

  const handleRepair = async () => {
    setIsRepairing(true);
    setStatusMessage(null);
    setErrorMessage(null);
    try {
      const repairUrl = apiBaseUrl
        ? `${apiBaseUrl.replace(/\/$/, "")}/internal/auth/repair`
        : "/internal/auth/repair";
      const response = await fetch(repairUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          payload.detail ?? payload.message ?? "Unable to repair API key",
        );
      }
      const fingerprint = payload.fingerprint
        ? ` (fingerprint ${payload.fingerprint})`
        : "";
      setStatusMessage(
        `API key regenerated ${fingerprint}. The UI should reload automatically due to an .env update...`,
      );
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Unable to repair API key";
      setErrorMessage(message);
    } finally {
      setIsRepairing(false);
    }
  };

  return (
    <div className="px-4 pt-4">
      <Alert className="flex flex-col items-center gap-2 border-slate-700 bg-slate-900 text-slate-50">
        <AlertTitle className="text-center text-base font-semibold tracking-wide">
          Skyvern API key missing
        </AlertTitle>
        <AlertDescription className="space-y-3 text-center text-sm leading-6">
          <p>
            All requests from the UI to the local backend will fail until a
            valid key is configured. Update <code>VITE_SKYVERN_API_KEY</code> in{" "}
            <code className="mx-1">skyvern-frontend/.env</code>
            by running <code>skyvern init</code> or click the button below to
            regenerate it automatically.
          </p>
          <div className="flex justify-center">
            <Button
              onClick={handleRepair}
              disabled={isRepairing}
              variant="secondary"
            >
              {isRepairing ? "Regenerating…" : "Regenerate API key"}
            </Button>
          </div>
          {statusMessage ? (
            <p className="text-xs text-slate-200">{statusMessage}</p>
          ) : null}
          {errorMessage ? (
            <p className="text-xs text-rose-200">{errorMessage}</p>
          ) : null}
        </AlertDescription>
      </Alert>
    </div>
  );
}

export { MissingApiKeyBanner };
