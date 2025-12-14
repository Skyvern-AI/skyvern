import { Input } from "@/components/ui/input";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";

type Props = {
  prompt?: string | null;
  downloadSuffix?: string | null;
  downloadTimeout?: number | null;
  errorCodeMapping?: Record<string, string> | null;
  maxRetries?: number | null;
  maxStepsPerRun?: number | null;
};

function FileDownloadBlockParameters({
  prompt,
  downloadSuffix,
  downloadTimeout,
  errorCodeMapping,
  maxRetries,
  maxStepsPerRun,
}: Props) {
  const formattedErrorCodeMapping = errorCodeMapping
    ? JSON.stringify(errorCodeMapping, null, 2)
    : null;

  return (
    <div className="space-y-4">
      {prompt ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Prompt</h1>
            <h2 className="text-base text-slate-400">
              Instructions followed to download the file
            </h2>
          </div>
          <AutoResizingTextarea value={prompt} readOnly />
        </div>
      ) : null}
      {downloadSuffix ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Download Suffix</h1>
            <h2 className="text-base text-slate-400">
              Expected suffix or filename for the downloaded file
            </h2>
          </div>
          <Input value={downloadSuffix} readOnly />
        </div>
      ) : null}
      {typeof downloadTimeout === "number" ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Download Timeout</h1>
            <h2 className="text-base text-slate-400">In seconds</h2>
          </div>
          <Input value={downloadTimeout.toString()} readOnly />
        </div>
      ) : null}
      {typeof maxRetries === "number" ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Max Retries</h1>
          </div>
          <Input value={maxRetries.toString()} readOnly />
        </div>
      ) : null}
      {typeof maxStepsPerRun === "number" ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Max Steps Per Run</h1>
          </div>
          <Input value={maxStepsPerRun.toString()} readOnly />
        </div>
      ) : null}
      {formattedErrorCodeMapping ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Error Code Mapping</h1>
          </div>
          <AutoResizingTextarea value={formattedErrorCodeMapping} readOnly />
        </div>
      ) : null}
      {!downloadSuffix &&
      typeof downloadTimeout !== "number" &&
      typeof maxRetries !== "number" &&
      typeof maxStepsPerRun !== "number" &&
      !formattedErrorCodeMapping ? (
        <div className="text-sm text-slate-400">
          No additional download-specific metadata configured for this block.
        </div>
      ) : null}
    </div>
  );
}

export { FileDownloadBlockParameters };
