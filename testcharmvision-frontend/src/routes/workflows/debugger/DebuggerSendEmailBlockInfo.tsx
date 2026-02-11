import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Input } from "@/components/ui/input";
import { HelpTooltip } from "@/components/HelpTooltip";

type Props = {
  recipients: Array<string>;
  body: string;
  subject: string;
};

function DebuggerSendEmailBlockParameters({
  recipients,
  body,
  subject,
}: Props) {
  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2">
        <div className="flex w-full items-center justify-start gap-2">
          <h1 className="text-sm">To</h1>
          <HelpTooltip content="The recipients of the email." />
        </div>
        <Input value={recipients.join(", ")} readOnly />
      </div>
      <div className="flex flex-col gap-2">
        <div className="flex w-full items-center justify-start gap-2">
          <h1 className="text-sm">Subject</h1>
          <HelpTooltip content="The subject of the email." />
        </div>
        <Input value={subject} readOnly />
      </div>
      <div className="flex flex-col gap-2">
        <div className="flex w-full items-center justify-start gap-2">
          <h1 className="text-sm">Body</h1>
          <HelpTooltip content="The body of the email." />
        </div>
        <AutoResizingTextarea value={body} readOnly />
      </div>
    </div>
  );
}

export { DebuggerSendEmailBlockParameters };
