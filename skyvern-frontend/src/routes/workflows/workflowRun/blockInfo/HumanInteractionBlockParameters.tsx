import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Input } from "@/components/ui/input";

type Props = {
  instructions: string | null;
  positiveDescriptor: string | null;
  negativeDescriptor: string | null;
  timeoutSeconds: number | null;
};

function HumanInteractionBlockParameters({
  instructions,
  positiveDescriptor,
  negativeDescriptor,
  timeoutSeconds,
}: Props) {
  return (
    <div className="space-y-4">
      {instructions ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Instructions</h1>
            <h2 className="text-base text-slate-400">
              Instructions for the human interaction
            </h2>
          </div>
          <AutoResizingTextarea value={instructions} readOnly />
        </div>
      ) : null}
      {positiveDescriptor ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Positive Descriptor</h1>
          </div>
          <Input value={positiveDescriptor} readOnly />
        </div>
      ) : null}
      {negativeDescriptor ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Negative Descriptor</h1>
          </div>
          <Input value={negativeDescriptor} readOnly />
        </div>
      ) : null}
      {typeof timeoutSeconds === "number" ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Timeout</h1>
            <h2 className="text-base text-slate-400">In seconds</h2>
          </div>
          <Input value={timeoutSeconds.toString()} readOnly />
        </div>
      ) : null}
    </div>
  );
}

export { HumanInteractionBlockParameters };
