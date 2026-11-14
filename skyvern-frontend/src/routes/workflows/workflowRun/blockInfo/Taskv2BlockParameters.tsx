import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Input } from "@/components/ui/input";

type Props = {
  prompt: string;
  url: string | null;
  maxSteps: number | null;
  totpVerificationUrl: string | null;
  totpIdentifier: string | null;
};

function Taskv2BlockParameters({
  prompt,
  url,
  maxSteps,
  totpVerificationUrl,
  totpIdentifier,
}: Props) {
  return (
    <div className="space-y-4">
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Prompt</h1>
          <h2 className="text-base text-muted-foreground">
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
    </div>
  );
}

export { Taskv2BlockParameters };
