import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

type Props = {
  format: string;
  landscape: boolean;
  printBackground: boolean;
  includeTimestamp: boolean;
  customFilename: string | null;
};

function PrintPageBlockParameters({
  format,
  landscape,
  printBackground,
  includeTimestamp,
  customFilename,
}: Props) {
  return (
    <div className="space-y-4">
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Format</h1>
        </div>
        <Input value={format} readOnly />
      </div>
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Landscape</h1>
        </div>
        <div className="flex w-full items-center gap-3">
          <Switch checked={landscape} disabled />
          <span className="text-sm text-slate-400">
            {landscape ? "Enabled" : "Disabled"}
          </span>
        </div>
      </div>
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Print Background</h1>
        </div>
        <div className="flex w-full items-center gap-3">
          <Switch checked={printBackground} disabled />
          <span className="text-sm text-slate-400">
            {printBackground ? "Enabled" : "Disabled"}
          </span>
        </div>
      </div>
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Include Timestamp</h1>
        </div>
        <div className="flex w-full items-center gap-3">
          <Switch checked={includeTimestamp} disabled />
          <span className="text-sm text-slate-400">
            {includeTimestamp ? "Enabled" : "Disabled"}
          </span>
        </div>
      </div>
      {customFilename ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Custom Filename</h1>
          </div>
          <Input value={customFilename} readOnly />
        </div>
      ) : null}
    </div>
  );
}

export { PrintPageBlockParameters };
