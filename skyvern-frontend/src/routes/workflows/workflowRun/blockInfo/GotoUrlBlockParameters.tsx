import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

type Props = {
  url: string;
  continueOnFailure: boolean;
};

function GotoUrlBlockParameters({ url, continueOnFailure }: Props) {
  return (
    <div className="space-y-4">
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">URL</h1>
          <h2 className="text-base text-slate-400">
            The destination Skyvern navigates to
          </h2>
        </div>
        <Input value={url} readOnly />
      </div>
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Continue on Failure</h1>
          <h2 className="text-base text-slate-400">
            Whether to continue if navigation fails
          </h2>
        </div>
        <div className="flex w-full items-center gap-3">
          <Switch checked={continueOnFailure} disabled />
          <span className="text-sm text-slate-400">
            {continueOnFailure ? "Enabled" : "Disabled"}
          </span>
        </div>
      </div>
    </div>
  );
}

export { GotoUrlBlockParameters };
