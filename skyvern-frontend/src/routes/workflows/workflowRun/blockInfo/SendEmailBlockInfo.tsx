import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Input } from "@/components/ui/input";

type Props = {
  recipients: Array<string>;
  body: string;
  subject: string;
};

function SendEmailBlockParameters({ recipients, body, subject }: Props) {
  return (
    <div className="space-y-4">
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">To</h1>
        </div>
        <Input value={recipients.join(", ")} readOnly />
      </div>
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Subject</h1>
        </div>
        <Input value={subject} readOnly />
      </div>
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Body</h1>
        </div>
        <AutoResizingTextarea value={body} readOnly />
      </div>
    </div>
  );
}

export { SendEmailBlockParameters };
