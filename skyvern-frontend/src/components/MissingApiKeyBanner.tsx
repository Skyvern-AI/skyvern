import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { envCredential } from "@/util/env";

function MissingApiKeyBanner() {
  if (import.meta.env.MODE !== "development") {
    return null;
  }

  const key = envCredential?.trim() ?? "";
  if (key.length > 0 && key !== "YOUR_API_KEY") {
    return null;
  }

  return (
    <div className="px-4 pt-4">
      <Alert className="flex flex-col items-center gap-2 border-slate-700 bg-slate-900 text-slate-50">
        <AlertTitle className="text-center text-base font-semibold tracking-wide">
          Skyvern API key missing
        </AlertTitle>
        <AlertDescription className="space-y-2 text-center text-sm leading-6">
          <p>
            All requests from the UI to the local backend will fail until a
            valid key is configured. Update <code>VITE_SKYVERN_API_KEY</code> in{" "}
            <code className="mx-1">skyvern-frontend/.env</code> by running{" "}
            <code>skyvern init</code> .
          </p>
        </AlertDescription>
      </Alert>
    </div>
  );
}

export { MissingApiKeyBanner };
