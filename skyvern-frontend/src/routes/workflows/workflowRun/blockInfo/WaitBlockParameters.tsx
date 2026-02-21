import { Input } from "@/components/ui/input";

type Props = {
  waitSec: number | null | undefined;
};

function WaitBlockParameters({ waitSec }: Props) {
  return (
    <div className="space-y-4">
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Wait Duration</h1>
          <h2 className="text-base text-slate-400">
            Seconds to wait before proceeding
          </h2>
        </div>
        <Input
          value={typeof waitSec === "number" ? `${waitSec}s` : "N/A"}
          readOnly
        />
      </div>
    </div>
  );
}

export { WaitBlockParameters };
