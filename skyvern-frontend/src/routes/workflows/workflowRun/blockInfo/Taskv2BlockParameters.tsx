import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

type Props = {
  prompt: string;
  url: string | null;
  maxSteps: number | null;
  totpVerificationUrl: string | null;
  totpIdentifier: string | null;
  disableCache: boolean;
};

function Taskv2BlockParameters({
  prompt,
  url,
  maxSteps,
  totpVerificationUrl,
  totpIdentifier,
  disableCache,
}: Props) {
  return (
    <div className="space-y-4">
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Prompt</h1>
          <h2 className="text-base text-slate-400">
            The instructions for this task
          </h2>
        </div>
        <AutoResizingTextarea value={prompt} readOnly />
      </div>
      {url ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">URL</h1>
          </div>
          <Input value={url} readOnly />
        </div>
      ) : null}
      {typeof maxSteps === "number" ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Max Steps</h1>
          </div>
          <Input value={maxSteps.toString()} readOnly />
        </div>
      ) : null}
      {totpVerificationUrl ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">TOTP Verification URL</h1>
          </div>
          <Input value={totpVerificationUrl} readOnly />
        </div>
      ) : null}
      {totpIdentifier ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">TOTP Identifier</h1>
          </div>
          <Input value={totpIdentifier} readOnly />
        </div>
      ) : null}
      {disableCache ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Cache Disabled</h1>
          </div>
          <div className="flex w-full items-center gap-3">
            <Switch checked={true} disabled />
            <span className="text-sm text-slate-400">
              Cache is disabled for this block
            </span>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export { Taskv2BlockParameters };
